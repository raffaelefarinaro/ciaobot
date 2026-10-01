"""Durable per-scope state and cheap applicability for "After this update" tasks.

What this is
------------
``ciao/update_task_catalog.py`` (issue #737) owns the *definitions*: a versioned,
cumulative, packaged catalog of the tasks an update asks for, each row naming the
detector that decides whether it applies, the check that decides whether it is
done, and the packaged prompt that does it. That module is inert by design — no
writes, no vault, no model. This is the half it was missing: where a task's
progress is *remembered*, and whether the task *applies right now*.

Two pieces, and no third
------------------------
1. A state store, scoped the way the catalog scopes a task. A
   ``scope == "workspace"`` task is remembered in that vault's own
   ``Workspace/Update-Tasks.json`` so a synced or backed-up vault carries the
   decision with it; a ``scope == "install"`` task is remembered in
   ``<runtime>/update-tasks.json``, beside the other per-install records. Both
   files are the same versioned document, ``{"schema": 1, "tasks": {...}}``, keyed
   by ``"<id>@<revision>"``.
2. An applicability layer that resolves a row's registered name to a function
   through ``DETECTOR_FUNCTIONS`` / ``COMPLETION_FUNCTIONS``, and a cached
   evaluation of every eligible task that neither walks the vault on every call
   nor ever claims more than it knows.

Chat launch, the API and the UI are the later children of #729. This module
never opens a chat: ``TaskState.chat_id`` is a field this child always leaves
empty, and nothing else here needs it.

Three rules everything else follows from
----------------------------------------
**Unknown is the answer when the answer is not known.** A detector name with no
implementation, a detector that raises, a detector that returns something other
than a :class:`Detection`, and a state file that exists but cannot be read all
resolve to ``unknown`` — never ``applicable``. Offering follow-up work this
engine cannot substantiate is the failure that wastes the operator's attention;
withholding a task that did apply is recoverable, and a later chat re-asks.
``not_applicable`` is a *positive* claim (the detector ran and the condition is
absent), which is why it needs evidence and ``unknown`` does not. An unreadable
state file outranks even a cached answer: the record is what says whether the
operator already declined the task, so an answer the install can no longer stand
behind must not stand in for it.

**Suppression is per revision.** A task dismissed or completed at revision 1
stays dismissed at revision 1 and is offered again at revision 2, because the
catalog's ``revision`` is how a maintainer says "the work behind this changed".
Suppression is therefore read from the record for the *exact* ``(id, revision)``
being evaluated, and a task this installed version cannot support is not offered
at all — while its record stays on disk, so a downgrade hides the task and
upgrading again brings it back with its history.

**Cheap is a contract, not a hope.** A detector may walk the vault; the layer
that calls it may not do that on every call. :func:`evaluate` runs the whole
check on ``ciao.async_reads.run_read`` (bounded, coalesced by key) and reuses the
previous result for ``APPLICABILITY_TTL_S``, or immediately when the caller
supplies a different workspace-change token. That window is **per task**: one
answer's freshness says nothing about another's, so a catalog that gained a task
does not extend the window of the tasks already there. The state file is re-read
on every call — it is one small JSON document, and a dismissal the operator just
made must not wait out a TTL — while the detectors, which are the part that can be
expensive, run at most once per fresh window. An answer that exists only because a
probe failed is not an answer about the workspace, so it is never cached.

#833's ``unrehomed-people`` detector is what makes that contract load-bearing
rather than theoretical: it is the one probe in the catalog that walks a vault, so
the window, the executor and the off-loop hop are the reason it is affordable at
all, and they are what a third detector that walks has to rely on too. It also
demonstrates the other half of the contract — a probe that can *prove* its answer
without walking should, and does: four cheap gates decide most installs, and the
walk is the last resort rather than the first step.

**A started task settles from evidence, not from prose.** A record in
:data:`SETTLING_LIFECYCLES` is an attempt nobody has judged yet, and judging it
is this layer's job rather than the chat's: :func:`evaluate` asks the row's
registered completion check, through :func:`record_completion`, once per fresh
window per task, and writes ``completed`` when the check's own postcondition
holds. Nothing else may write that lifecycle — not the agent, not a ``done=true``,
not the closure of a chat — and that is the whole reason a reviewed no-op is a
finished task instead of an offer that never goes away. The settlement rides the
*same* window as the detector rather than adding a second clock, on purpose: a
task whose evidence lands mid-window is noticed within
:data:`APPLICABILITY_TTL_S`, which is the answer this module is willing to give
about how quickly it looks at the world, and no faster claim is made anywhere.

Two consequences stated rather than hidden. The freshness window is a *named
constant*, not a setting and not an env var: a per-install knob for "how stale may
an answer be" is not a decision an operator has ever asked to make, and
``AGENTS.md`` forbids a new env var where a constant does. And every name a
catalog row may use is a name this engine can act on — #728-E's
``learnings-cleanup`` and, since #833, the install-scoped ``unrehomed-people``
re-home — so the loader refuses anything else, and a probe with no implementation
behind it still answers ``unknown`` rather than ``applicable``.

**A task may share a surface's answer without sharing its evidence.** #833's
``unrehomed-people`` is about the same receipt the OS audit reports as
``unrehomed_people``, and it is the first pair here where the two disagree about
what happens next: the card can be dismissed or completed and disappear, the
report cannot be silenced at all. So the probes call
:mod:`ciao.migration_notices` rather than restating anything, and nothing in that
module reads a task record — which is what keeps "the card is gone" and "the
audit is quiet" from being the same sentence.

What the two do **not** share is the strength of the evidence, and the difference
is the point. The audit's answer is a receipt check, cheap and broad, and it stays
that way: a diagnostic should keep reporting a migration nobody ever needed. A
*card* is an offer to do work, so it needs a reason that survives an operator's
"why am I seeing this?" — and #833's first pass answered it with the receipt check,
which offered the task on every fresh conforming install. Its detector therefore
runs :func:`ciao.migration_notices.rehome_legacy_candidates`, a bounded plan walk
that establishes a real candidate, and it is the only applicability probe in this
catalog that reads the vault. The asymmetry is one-directional and pinned by a
test:

    task offered  =>  audit reports
    audit silent  =>  task not offered

Nothing widens a task to make it match a notice. That direction is how #833's
review happened.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ciao.async_reads import keyed_lock, run_read
from ciao.learning_records import LEARNINGS_RELATIVE
from ciao.memory_receipts import content_revision
from ciao.update_task_catalog import TaskCatalog, UpdateTask, load_catalog

logger = logging.getLogger(__name__)

# ── The state document ──────────────────────────────────────────────────────

#: Schema version stamped into every state file. A document carrying any other
#: value is a schema this code does not implement, and is read as unreadable
#: rather than half-understood — the same rule ``update_task_catalog`` applies to
#: a future catalog.
STATE_SCHEMA = 1

#: Where the pipeline writes its own bookkeeping inside a vault. Shared by name
#: with ``vault_index``'s reserved-file rule, so a user's note called
#: ``Update-Tasks.json`` somewhere else stays indexed.
WORKSPACE_STATE_DIR = "Workspace"

#: The workspace state file, relative to that directory. Opened at exactly this
#: spelling, which is also the spelling ``vault_index`` matches casefolded in its
#: reserved-file set: this module writes one name and the index recognizes it.
WORKSPACE_STATE_FILENAME = "Update-Tasks.json"

#: The install state file, in the runtime directory beside ``state.json``.
INSTALL_STATE_FILENAME = "update-tasks.json"

#: The lifecycles a record may hold. ``offered`` and ``in_progress`` are what an
#: undecided task is; ``waiting_review`` is a task whose chat is still open;
#: ``failed`` is an attempt that did not complete; the last two are the operator's
#: decision or the check's verdict.
LIFECYCLES: frozenset[str] = frozenset(
    {
        "offered",
        "in_progress",
        "waiting_review",
        "failed",
        "completed",
        "dismissed",
    }
)

#: The lifecycles that suppress an offer at the *same* revision. A record in
#: another lifecycle is an unfinished attempt, not a decision, and is offered
#: again.
SUPPRESSING_LIFECYCLES: frozenset[str] = frozenset({"completed", "dismissed"})

#: The lifecycles whose record is an attempt that may already have produced its
#: evidence. These are the ones :func:`_settle` asks the completion check about.
#:
#: * ``in_progress`` — the prompt is in a chat and the chat may have done the
#:   work by now.
#: * ``waiting_review`` — the chat is parked on a decision, and that decision
#:   has since been made.
#: * ``failed`` — the turn never reached its chat, so the answer here is
#:   "nothing to settle" rather than a verdict, and the record keeps saying the
#:   task failed. Including it is what stops a retry from being needed just to
#:   find out whether the earlier attempt finished after all.
#:
#: ``offered`` is absent because there is nothing to judge: no chat was ever
#: opened, so no evidence can exist. ``completed`` and ``dismissed`` are absent
#: because they are decisions already — a dismissal is the operator's, and a
#: verdict is a check's, and re-asking a check whether its own verdict still
#: holds would reopen a decision on a timer.
SETTLING_LIFECYCLES: frozenset[str] = frozenset(
    {"in_progress", "waiting_review", "failed"}
)

# ── Applicability ───────────────────────────────────────────────────────────

#: The task applies. A positive claim, so it is only ever produced by a detector
#: that ran and returned evidence.
APPLICABLE = "applicable"

#: The task does not apply: the detector ran and the condition is absent. Also a
#: positive claim, about the absence.
NOT_APPLICABLE = "not_applicable"

#: Nobody can say. An absent or failing detector, a detector that returned
#: something else, a state file that exists but cannot be read.
UNKNOWN = "unknown"

#: The three answers, as a set a caller can test membership on.
APPLICABILITY_STATUSES: frozenset[str] = frozenset(
    {APPLICABLE, NOT_APPLICABLE, UNKNOWN}
)

#: The ``reason`` values whose answers are never cached. A detector that raised,
#: or one that returned something that is not a :class:`Detection`, says nothing
#: about the workspace — it is a transient fault or a bug in the detector, and
#: re-running it on the next call costs one probe. Caching one for
#: ``APPLICABILITY_TTL_S`` would hide a condition the operator is waiting for
#: behind an error that had already gone away.
#:
#: ``detector_not_implemented`` is deliberately *not* here: nothing about the
#: workspace will change until an implementation ships, so the same answer for
#: the whole window is the honest one.
UNCACHEABLE_REASONS: frozenset[str] = frozenset(
    {"detector_failed", "detector_returned_no_detection", "state_unreadable"}
)

#: How long a computed applicability may be reused, in seconds, before the
#: detectors run again. Named, finite, and a constant rather than a setting: a
#: Home render must not block on a detector, and five minutes of staleness on a
#: condition the operator completes by hand is not worth another knob. A caller
#: that knows the workspace changed passes a different ``change_token`` and
#: invalidates immediately instead of waiting.
APPLICABILITY_TTL_S = 300.0

#: Hex characters kept from the digest behind ``attempted_fingerprint``. Enough
#: to say "exactly the attempt we already made", short enough to read in a file.
FINGERPRINT_CHARS = 16


@dataclass(frozen=True, slots=True)
class Detection:
    """What one named, cheap function concluded, and what it saw to conclude it.

    A completion check returns the same shape, where ``applicable`` means the
    task's postcondition holds. One type for both keeps the dispatcher in
    :func:`apply_detector` and the gate in :func:`record_completion` identical,
    and keeps a check from inventing a second answer vocabulary.
    """

    applicable: bool
    evidence: dict[str, Any] = field(default_factory=dict)


#: A detector or a completion check. Called as keyword arguments only —
#: ``config``, ``workspace`` and ``today`` — so a function cannot be silently
#: handed a different context by a positional call, and so adding an argument
#: later is a deliberate change every implementation sees.
Probe = Callable[..., Detection]

#: The registries live at the foot of the probe section below, next to the
#: functions they name.


# ── The Learnings cleanup probes (#728-E) ───────────────────────────────────


def _learnings_cleanup_review_needed(
    *, config: Any, workspace: str = "", today: date | None = None
) -> Detection:
    """Whether this workspace has learnings entries only a person can judge.

    Applicable exactly when the reconciliation has rows it will *not* retire —
    an entry nothing has ever proposed, one whose finding is still open, one
    routed only to an upstream issue, or a line the parser cannot read. Those are
    the rows the attended ``ciao learnings-cleanup`` workflow exists for, and an
    empty set of them means the unattended pass is already doing everything this
    engine can do on its own, so there is nothing to offer.

    The evidence is the counts, not a boolean, so the fingerprint changes when a
    new entry appears and the task comes back — which is the behaviour a person
    wants: a document that gained a lesson is a document that has not been
    reviewed.

    Never a false negative that hides a row: an exception becomes
    ``Detection(applicable=False, …)`` only where the underlying planner has
    already refused to act (:attr:`~ciao.learnings_cleanup.CleanupPlan.blocked`),
    and a genuine failure raises so :func:`apply_detector` records ``unknown``.
    """
    from ciao import learnings_cleanup

    vault_root = _workspace_vault(config, workspace)
    if vault_root is None:
        raise ValueError("no workspace vault is registered for this task")
    plan = learnings_cleanup.plan_cleanup(
        vault_root, workspace=workspace, config=config, today=today
    )
    if plan.blocked:
        return Detection(False, {"blocked": plan.blocked})
    counts = plan.counts
    outstanding = counts["keep"] + counts["conflict"]
    return Detection(
        outstanding > 0,
        {
            "active": counts["active"],
            "removable": counts["remove"],
            "kept": counts["keep"],
            "unreadable": counts["conflict"],
            "unmatched": counts["unmatched"],
            "reasons": sorted({row.reason for row in plan.kept}),
            "revision": plan.revision,
        },
    )


def _review_is_attended(receipt: dict[str, Any]) -> bool:
    """Whether a person stands behind this receipt, rather than a flag.

    Three shapes of receipt land in the same directory and only one of them is a
    review of anything:

    * the attended workflow, ``ciao learnings-cleanup --apply --approval-file``,
      which records ``reviewed`` — or names the rows it approved, each with the
      reason and the evidence a person wrote for it;
    * the nightly pass, ``--apply-settled``, which retires the rows the fold
      already proposed and certifies nothing. It writes a receipt too, because a
      removal without one could not be reversed, so "a receipt exists" is not the
      test — ``removed_by`` is in every receipt for exactly this question;
    * a run that persisted its receipt and then failed at, or died before, the
      document write. Nothing was reviewed and nothing was removed.

    The last two are the reason this is a positive test on ``reviewed``/``approvals``
    rather than a check that the receipt is merely well-formed. A completion check
    that a flag can satisfy is not a review, and the task it closes is the one
    asking a person to look.
    """
    if str(receipt.get("removed_by") or "") == "system":
        return False
    return bool(receipt.get("reviewed") or receipt.get("approvals"))


def _learnings_cleanup_review_recorded(
    *, config: Any, workspace: str = "", today: date | None = None
) -> Detection:
    """Whether a durable cleanup receipt exists for the document as it stands.

    A receipt, not a chat. The postcondition is "a person looked at this file as
    it is now and said what should go", and the only durable record of that is the
    receipt ``ciao learnings-cleanup --apply --approval-file`` writes — including
    when it removed nothing, which is why a fully reviewed no-op completes and
    generating the table does not.

    "As it stands" is checked, not assumed, and it is checked on the right side of
    the receipt's own write. A receipt names two revisions: a run that removed
    something is only a review of the document that is there now when the write
    *landed*, so it must be ``revision_after``. Its ``revision_before`` is the
    revision the file still has precisely when the write did not happen — a
    receipt persisted by a run that then failed is a review of nothing, and
    accepting that side is how a run that removed no bytes at all comes to
    certify a cleanup. A no-op removed nothing, so there was no second write and
    both sides name the reviewed revision; it matches on ``revision_before``.

    A receipt for a document that has since been edited is not a review of this
    document, and treating it as one is how a stale cleanup comes to certify
    itself.
    """
    from ciao.learnings_cleanup import read_receipt

    del today  # the check is about the file, not about a date
    vault_root = _workspace_vault(config, workspace)
    if vault_root is None:
        raise ValueError("no workspace vault is registered for this task")
    current = _learnings_revision(vault_root)
    if current is None:
        return Detection(False, {"reason": "no_learnings_document"})

    directory = _runtime_root(config) / "migration"
    if not directory.is_dir():
        return Detection(False, {"reason": "no_review_receipt", "revision": current})
    newest: dict[str, Any] | None = None
    for path in sorted(directory.glob("learnings-cleanup-*.json")):
        receipt = read_receipt(path)
        if receipt is None or str(receipt.get("workspace") or "") != workspace:
            continue
        if not _review_is_attended(receipt):
            continue
        side = (
            "revision_before"
            if int(receipt.get("entries_removed") or 0) == 0
            else "revision_after"
        )
        if str(receipt.get(side) or "") != current:
            continue
        if newest is None or str(receipt.get("removed_at") or "") > str(
            newest.get("removed_at") or ""
        ):
            newest = receipt
    if newest is None:
        return Detection(False, {"reason": "no_review_receipt", "revision": current})
    return Detection(
        True,
        {
            "receipt": newest.get("removed_at", ""),
            "removed": int(newest.get("entries_removed") or 0),
            "approvals": len(newest.get("approvals") or {}),
            "revision": current,
        },
    )


# ── The person re-home probes (#833) ─────────────────────────────────────────


def _unrehomed_people_rehome_needed(
    *, config: Any, workspace: str = "", today: date | None = None
) -> Detection:
    """Whether this install has **real** legacy misfiled person notes to re-home.

    Deliberately narrower than the ``unrehomed_people`` **notice** the audit
    reports, and the difference is the whole of this probe. The notice is a
    diagnostic that says "a re-homing has never been recorded here", which is
    true of every fresh install that never needed one; this is an offer to do
    work, so it needs something to do. #833's first pass used the notice's
    condition and the review was right: a fresh conforming multi-workspace
    install with no legacy data was offered a task, which is precisely what #800
    says a task must not be.

    So the evidence is the plan itself — :func:`ciao.vault_rehome.plan_rehome` on
    the shared vault, over the registry's workspace names — and only a
    **mechanical** candidate counts: a person note whose tags name another
    registered workspace, which is the damage the old global curation run did. An
    untagged note is a judgement case about a relationship with a person, and
    untagged contacts are ordinary on a fresh install, so they are never evidence.
    Conflicts are counted but not offered either: they are tag-obvious notes whose
    destination is occupied, which is a content decision the command refuses.

    Four gates run before the walk and each one *proves* a mechanical candidate is
    unreachable (a completed receipt, one workspace, no shared vault directory,
    no bound tag role) — so the common answer, "nothing to move", costs no vault
    read at all. See :func:`ciao.migration_notices.rehome_legacy_candidates`,
    which is where that reasoning lives. **One** bound role is enough for a real
    move: a note tagged for a role that binds to a workspace other than its own
    moves even when its own workspace plays no role at all, which is the case a
    "two roles or more" gate got wrong.

    **This probe walks the vault, and the layer is what makes that affordable.**
    :func:`evaluate` runs it off the event loop through
    :func:`ciao.async_reads.run_read` — coalesced per install, admission-capped
    with every other vault read — and at most once per fresh window per task, so a
    full plan costs one walk per ``APPLICABILITY_TTL_S`` and no more. This is the
    only applicability probe in the catalog that walks anything, and it is the
    price of not offering the task on an install with nothing to move. Nothing
    polled (``operator_actions``, the audit) calls it: they use the notice.

    The evidence carries the candidate paths and the counts, all of them
    vault-relative or scalar so the record stays portable, and the fingerprint
    moves when the candidate set does — a workspace that gains a second
    misfiled contact brings the task back with a new fingerprint.

    A raise is not a "no": an unreadable registry, a failing receipt read or a
    config with no vault root propagates, and :func:`apply_detector` records
    ``unknown``. Silence from evidence this install could not read would be a
    claim it cannot make, in the one direction where the operator would act on it.
    """
    del workspace, today  # install-scoped: neither argument is about one workspace
    from ciao.migration_notices import rehome_legacy_candidates

    evidence = rehome_legacy_candidates(config, _runtime_root(config))
    return Detection(
        bool(evidence.mechanical),
        {
            "reason": evidence.reason,
            "mechanical": list(evidence.mechanical),
            "conflicts": evidence.conflicts,
            "needs_judgement": evidence.needs_judgement,
            "notes_scanned": evidence.notes_scanned,
            "scanned": evidence.scanned,
        },
    )


def _unrehomed_people_rehome_recorded(
    *, config: Any, workspace: str = "", today: date | None = None
) -> Detection:
    """Whether a completed person re-homing has been recorded for this install.

    The receipt and nothing else. Opening a chat is not completion, and neither
    is silence, and neither is the notes looking right: this check is the
    registered postcondition the remedy actually satisfies, and
    :func:`~ciao.vault_rehome.rehome_people` writes it.

    It goes through the same :func:`~ciao.migration_notices.completed_rehome` the
    notice's absence half uses, so "the notice is silent" and "the task is done"
    are one fact rather than two readers of one file. That reader is the canonical
    completed-only accessor, which is what makes the cases honest:

    * a ``partial`` receipt is **not** completion — a run that could not move
      some note left the vault half re-homed, and every reference to the note it
      never moved already points at a path it is not at.
    * a receipt predating the ``status`` field **is** completion, because those
      installs did the work.
    * a missing, unparseable or unreadable receipt is **not** completion. Absence
      of proof is not proof, and this is the direction where that matters: a
      receipt this install cannot read must not certify work nobody can show.

    **An honest no-op run completes this task.** A first ``--apply`` over a
    vault with nothing to move writes ``status: migrated`` with an empty move
    list — ``rehome_people.should_record`` includes ``recorded is None`` — which
    is a real run that really did look, and it is the only route that settles the
    condition on a re-rooted install where the notes are already per-workspace and
    the plan legitimately finds nothing. That is a fact about the vault, not a
    trick to satisfy a check.

    Install-scoped, as above: the receipt is per install.
    """
    del workspace, today  # install-scoped: the receipt is not a workspace's
    from ciao.migration_notices import completed_rehome

    receipt = completed_rehome(_runtime_root(config))
    if receipt is None:
        return Detection(False, {"reason": "no_completed_rehome_receipt"})
    return Detection(
        True,
        {
            "receipt": str(receipt.get("rehomed_at") or ""),
            "moved": len(receipt.get("moves") or []),
            "rewritten": len(receipt.get("rewrites") or []),
            # The receipt's own `vault_root` is deliberately not carried: it is
            # an absolute path, and state files travel between machines.
        },
    )


def _workspace_vault(config: Any, workspace: str) -> Path | None:
    """The vault root of one workspace, or ``None`` when nothing resolves it.

    Raised-through rather than substituted: every caller here would otherwise
    have to decide what an unresolvable workspace means, and the two honest
    answers — ``not_applicable`` for a task with nothing to review, ``unknown``
    for one whose input could not be read — are different. A workspace that is
    not registered is a fault in the caller, not a state of the vault.
    """
    try:
        return Path(config.workspace_vault_root(workspace))
    except Exception:  # noqa: BLE001 — a registry that cannot answer is a fault
        return None


def _runtime_root(config: Any) -> Path:
    """The install's runtime directory, taken from the state file beside it.

    ``config.state_path`` is ``<runtime>/state.json`` on every layout this ships,
    so its parent is the runtime root without this module having to know where
    the runtime root is configured — which is exactly the kind of knowledge that
    goes stale.
    """
    return Path(config.state_path).parent


def _learnings_revision(vault_root: Path) -> str | None:
    """The document's whole-file revision, or ``None`` when there is no file.

    Decoded from bytes for the same reason every other read of this file is: a
    CRLF document hashed through ``read_text`` would not match the revision the
    cleanup receipt recorded, and the check would then never be satisfied for a
    Windows-authored vault.
    """
    path = vault_root / LEARNINGS_RELATIVE
    if not path.is_file():
        return None
    try:
        return content_revision(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


#: Every detector this engine ships. All are read-only, and all are registered in
#: :data:`ciao.update_task_catalog.DETECTORS`, so a catalog row cannot name
#: anything this engine has no code for. The functions live here rather than in
#: the module they are about, so the registry is readable in one place; their
#: bodies import lazily, because a Home render that does not touch Learnings or
#: a vault should not pay for either module.
DETECTOR_FUNCTIONS: dict[str, Probe] = {
    "learnings-cleanup-review-needed": _learnings_cleanup_review_needed,
    "unrehomed-people-review-needed": _unrehomed_people_rehome_needed,
}

#: Registered completion checks, same contract. A name with no implementation
#: means :func:`record_completion` records nothing, which is the point: a task is
#: never marked done because nobody wrote the check that would prove it.
COMPLETION_FUNCTIONS: dict[str, Probe] = {
    "learnings-cleanup-review-recorded": _learnings_cleanup_review_recorded,
    "unrehomed-people-rehome-recorded": _unrehomed_people_rehome_recorded,
}


@dataclass(frozen=True, slots=True)
class ApplicabilityResult:
    """One task's answer, its evidence, and the fingerprint of both.

    ``fingerprint`` is a digest over the task's identity, its detector name, the
    status and the canonical evidence, so it changes when the *situation* changes
    and not when the wording does. That is what makes it safe to store as
    ``attempted_fingerprint``: it says "an attempt was made against exactly this
    condition", not "an attempt was made once".

    ``checked_at`` is the wall-clock moment *this answer was computed* — when the
    detector ran, or when a failure stopped one from being able to run. It is
    neither evidence nor part of the fingerprint, because a second list inside the
    freshness window is the same answer and must not read as a new situation. A
    cached hit keeps the stamp of the call that computed it, which is the only
    honest reading: the answer was not re-derived, and re-stamping it on read
    would date every task to the last listing rather than the last check. It is
    the "when did anyone last look" half of a task's history, kept separate from
    the record's ``updated_at``, which is the "when did the operator decide"
    half — the two say different things and a surface that merges them is lying
    about one of them.
    """

    status: str
    evidence: dict[str, Any]
    fingerprint: str
    checked_at: str = ""


@dataclass(frozen=True, slots=True)
class TaskState:
    """What is remembered about one task, in one scope, at one revision.

    Everything here is portable: the file this is written to may be synced,
    backed up, read by hand, and moved to another machine, so no field may hold
    an absolute path. The record's own identity is ``(task_id, revision)`` and
    nothing else — a scope holds many workspaces' worth of nothing but the
    workspace's own file does.
    """

    task_id: str
    revision: int
    scope: str
    lifecycle: str
    updated_at: str
    attempted_fingerprint: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    # The chat a later child (#729-C) opened for this task. Always empty here:
    # this module never launches a chat, and writing the field now keeps the
    # document shape from changing when that child lands.
    chat_id: str = ""
    # The digest of the prompt the task was offered with, so a re-offer at a new
    # revision can say whether the instructions changed.
    prompt_digest: str = ""


@dataclass(frozen=True, slots=True)
class TaskStatus:
    """One eligible task with its answer and its state, for a caller to render.

    ``suppressed`` and ``offered`` are derived, never stored, so the two answers
    cannot disagree with the record they came from: ``offered`` is the one the
    Housekeeping card will ask about, and it is ``applicable`` *and* not
    suppressed at this exact revision.
    """

    task: UpdateTask
    applicability: ApplicabilityResult
    state: TaskState | None = None

    @property
    def suppressed(self) -> bool:
        """True when a record at this exact revision decided the task is done."""
        return self.state is not None and self.state.lifecycle in SUPPRESSING_LIFECYCLES

    @property
    def offered(self) -> bool:
        """True when this task should be put in front of the operator now."""
        return self.applicability.status == APPLICABLE and not self.suppressed


class UpdateTaskStateError(ValueError):
    """A state record cannot be stored as written.

    Raised rather than repaired, because the property it protects — nothing
    absolute in a file that travels — cannot be fixed by guessing which of two
    spellings was meant.
    """


# ── Where the state lives ───────────────────────────────────────────────────


def state_path_for(config: Any, scope: str, workspace: str = "") -> Path:
    """The state file one scope reads and writes.

    A workspace task's decisions live in that workspace's own vault, so a backup
    or a sync of the vault carries the operator's dismissal with it and a second
    install of the same vault agrees about what was declined. An install task's
    decisions live in the runtime directory, beside the other records that
    describe this install rather than a workspace.

    Raises ``ValueError`` for a scope that is neither. That is a caller's bug
    rather than a condition to absorb: the catalog validates ``scope`` at load,
    so an unknown value here means a row reached this layer without the loader.
    """
    if scope == "install":
        return Path(config.state_path).parent / INSTALL_STATE_FILENAME
    if scope != "workspace":
        raise ValueError(f"unknown update-task scope {scope!r}")
    vault = Path(config.workspace_vault_root(workspace))
    return vault / WORKSPACE_STATE_DIR / WORKSPACE_STATE_FILENAME


def task_key(task_id: str, revision: int) -> str:
    """The key one ``(id, revision)`` record is stored under."""
    return f"{task_id}@{revision}"


# ── Reading ─────────────────────────────────────────────────────────────────


def _read_document(path: Path) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return ``(records, readable)`` for one state file.

    A file that is not there is a fresh scope that has decided nothing, which is
    readable and empty — the ordinary state of a new install. A file that is
    there and cannot be understood (malformed JSON, not an object, a schema this
    code does not implement, ``tasks`` not a mapping) is unreadable, and the
    caller answers ``unknown`` rather than re-offering whatever the operator
    decided. Every filesystem and decode error lands on the unreadable branch
    too: this runs on a Home render, where raising is the worse outcome.

    An entry under ``tasks`` that is not an object is dropped rather than kept:
    it is not a record, and carrying it forward would mean guessing what it was
    meant to be.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return {}, False
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        return {}, False
    tasks = raw.get("tasks")
    if not isinstance(tasks, dict):
        return {}, False
    return (
        {
            key: value
            for key, value in tasks.items()
            if isinstance(key, str) and isinstance(value, dict)
        },
        True,
    )


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def _parse_state(
    record: Mapping[str, Any] | None, task: UpdateTask
) -> TaskState | None:
    """Return the stored state for ``task``, or ``None`` when there is none.

    A record whose lifecycle this code does not know, or whose own identity
    disagrees with the key it was found under, is read as *absent* rather than
    guessed at. Offering a task the operator believes is finished is an
    annoyance; silently honouring an unfamiliar lifecycle is an unrequested
    decision, and the write that produced it is somewhere else.
    """
    if record is None:
        return None
    lifecycle = _text(record.get("lifecycle"))
    if lifecycle not in LIFECYCLES:
        return None
    if _text(record.get("task_id"), task.id) != task.id:
        return None
    revision = record.get("revision", task.revision)
    if isinstance(revision, bool) or not isinstance(revision, int):
        return None
    if revision != task.revision:
        return None
    evidence = record.get("evidence")
    return TaskState(
        task_id=task.id,
        revision=task.revision,
        scope=_text(record.get("scope"), task.scope),
        lifecycle=lifecycle,
        updated_at=_text(record.get("updated_at")),
        attempted_fingerprint=_text(record.get("attempted_fingerprint")),
        evidence=dict(evidence) if isinstance(evidence, dict) else {},
        chat_id=_text(record.get("chat_id")),
        prompt_digest=_text(record.get("prompt_digest")),
    )


def _load_states(
    tasks: tuple[UpdateTask, ...] | list[UpdateTask], config: Any, workspace: str
) -> tuple[dict[tuple[str, int], TaskState | None], set[str]]:
    """Read each scope's document once and return the per-task state and bad scopes.

    The unreadable scopes come back as a set of scope keys so the caller can
    answer ``unknown`` for their tasks *before* running a detector, rather than
    computing a claim it is not allowed to make.
    """
    documents: dict[str, dict[str, dict[str, Any]]] = {}
    unreadable: set[str] = set()
    out: dict[tuple[str, int], TaskState | None] = {}
    for task in tasks:
        key = _scope_key(config, task, workspace)
        document = documents.get(key)
        if document is None:
            path = state_path_for(config, task.scope, workspace)
            records, readable = _read_document(path)
            document = records
            documents[key] = records
            if not readable:
                unreadable.add(key)
        out[(task.id, task.revision)] = (
            None
            if key in unreadable
            else _parse_state(document.get(task_key(task.id, task.revision)), task)
        )
    return out, unreadable


def read_task_state(
    task: UpdateTask, *, config: Any, workspace: str = ""
) -> TaskState | None:
    """The state recorded for this exact ``(id, revision)``, or ``None``.

    ``None`` covers both "nothing has been decided" and "what was decided cannot
    be read"; :func:`evaluate` tells those apart, because only the second one
    withholds an answer.

    Takes no lock: the document is replaced atomically, so a lock-free reader sees
    a whole one, and a reader that needed the write lock would make every Home
    render queue behind a write. A caller that is about to *write* this record
    must read it through :func:`_record_lock` instead.
    """
    return _read_record(state_path_for(config, task.scope, workspace), task)


def _read_record(path: Path, task: UpdateTask) -> TaskState | None:
    """The one record for *task* in one state file, or ``None``."""
    records, readable = _read_document(path)
    if not readable:
        return None
    return _parse_state(records.get(task_key(task.id, task.revision)), task)


# ── Writing ─────────────────────────────────────────────────────────────────


def _portable_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the evidence entries a state file may hold.

    Storable means a JSON scalar, or a list/dict of them, with every string
    relative. An entry that is not storable is dropped rather than repaired: it
    is one detector's private debug value, and the record's job is to say what
    was decided, not to carry a path from this machine.
    """
    out: dict[str, Any] = {}
    for key, value in evidence.items():
        if not isinstance(key, str):
            continue
        if _portable(value):
            out[key] = value
    return out


def _portable(value: Any, depth: int = 0) -> bool:
    if depth > 4:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return not _looks_absolute(value)
    if isinstance(value, (list, tuple)):
        return all(_portable(item, depth + 1) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _portable(item, depth + 1)
            for key, item in value.items()
        )
    return False


#: A Windows drive letter followed by a separator, checked on a POSIX install:
#: `os.path.isabs` does not recognise `C:\Users\...` and a synced vault is opened
#: on both. The separator is part of the pattern on purpose — `a: not relevant`
#: and `x:y` are ordinary text, and a rule that called them paths would refuse a
#: dismissal reason the operator actually typed.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _looks_absolute(value: str) -> bool:
    """True for a spelling that names a location on this (or any) machine.

    Deliberately a short list rather than ``os.path.isabs`` and a colon test: this
    guards one property of a portable file, and a broader rule would reject
    evidence nobody meant as a path. What counts is a rooted POSIX path, a
    rooted Windows path (including a UNC ``\\\\server`` share), a home-relative
    ``~`` path, a Windows drive with a separator, or a NUL — which is not text.
    """
    if "\0" in value:
        return True
    text = value.strip()
    if not text:
        return False  # an empty string names nothing, so it stores fine
    if text in ("~",) or text.startswith(("~/", "~\\")):
        return True
    if text.startswith(("/", "\\")):
        return True
    return bool(_WINDOWS_DRIVE_RE.match(text))


def _reject_nonportable(evidence: Mapping[str, Any]) -> None:
    """Raise when evidence would put a machine-specific path in a portable file."""
    for key, value in evidence.items():
        if not isinstance(key, str) or not _portable(value):
            raise UpdateTaskStateError(
                f"update-task state evidence {key!r} is not storable: state files "
                "travel between machines, so they hold no absolute paths, no "
                "path-like values and nothing deeper than four levels"
            )


def _state_payload(state: TaskState) -> dict[str, Any]:
    """The record as it is written, after the portability check."""
    _reject_nonportable(state.evidence)
    return {
        "task_id": state.task_id,
        "revision": state.revision,
        "scope": state.scope,
        "lifecycle": state.lifecycle,
        "updated_at": state.updated_at,
        "attempted_fingerprint": state.attempted_fingerprint,
        "evidence": state.evidence,
        "chat_id": state.chat_id,
        "prompt_digest": state.prompt_digest,
    }


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    """Write the whole state document atomically.

    A unique temp name beside the target, then one ``os.replace``, and the temp
    file is cleaned up on every path out. A reader therefore sees either the
    previous document or the new one, never a half-written one — and the
    half-written one would be *unreadable*, which this design answers ``unknown``
    and which would hide every task in the scope until the next write. Keys are
    sorted, so writing an unchanged document produces identical bytes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    text = json.dumps(document, indent=2, sort_keys=True, default=str) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".update-tasks.", suffix=".tmp")
    temporary = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        # Before the rename, so the file never appears carrying mkstemp's 0600
        # instead of the mode a vault sync and a hand read expect.
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        # A no-op once the rename landed, a cleanup when it did not.
        temporary.unlink(missing_ok=True)


def write_task_state(state: TaskState, *, config: Any, workspace: str = "") -> Path:
    """Store one record and return the file it landed in.

    The whole document is read and rewritten under ``keyed_lock``, so a
    concurrent record for a different task in the same scope is not lost — the
    read-modify-write is the whole reason the lock is process-wide rather than a
    lock on the file. Writing the same state twice is the same file, byte for
    byte: the record is keyed by identity, the dump is sorted, and no field is
    derived from the clock inside here.

    A caller that needs the *previous* record to build this one must not read it
    first and then call this: the read belongs inside the same critical section,
    which is what :func:`_record_lock` is for. This entry point deliberately
    takes the lock itself rather than delegating, because ``keyed_lock`` is a
    plain (non-reentrant) lock and a nested acquisition of the same key would
    deadlock.

    Raises ``UpdateTaskStateError`` when the evidence is not storable, and
    ``OSError`` when the write cannot land. Neither is swallowed: a dismissal
    that did not happen must not be reported as one.
    """
    if state.lifecycle not in LIFECYCLES:
        raise UpdateTaskStateError(
            f"{state.lifecycle!r} is not an update-task lifecycle "
            f"({', '.join(sorted(LIFECYCLES))})"
        )
    path = state_path_for(config, state.scope, workspace)
    with keyed_lock(_lock_key(path)):
        _write_record(path, state)
    return path


def _lock_key(path: Path) -> str:
    """The ``keyed_lock`` name for one state file."""
    return f"update-tasks:{path}"


def _write_record(path: Path, state: TaskState) -> None:
    """Merge one record into the document and write it. The caller holds the lock.

    Refuses to build a new document on top of one this code could not read: the
    records in it may be the operator's only copy of a decision, and this module
    has no way to carry them forward.
    """
    payload = _state_payload(state)
    records, readable = _read_document(path)
    if not readable and path.exists():
        raise UpdateTaskStateError(
            f"the update-task state at {path.name} is not a readable "
            "schema-1 document; refusing to overwrite it"
        )
    records[task_key(state.task_id, state.revision)] = payload
    _write_document(path, {"schema": STATE_SCHEMA, "tasks": records})


@contextmanager
def _record_lock(
    task: UpdateTask, config: Any, workspace: str
) -> Iterator[tuple[Path, TaskState | None]]:
    """Hold one scope's write lock while the caller reads and replaces a record.

    Yields the state file and the record currently stored for *task*, and expects
    the caller to write through :func:`_write_record` (or not write at all)
    before the lock is released.

    The read and the write have to be the same critical section. A record is the
    whole document, so reading it outside the lock and carrying the result into a
    later write loses whatever a concurrent writer added in between — the chat id
    the launch path sets, the attempt fingerprint the check path sets — with no
    error anywhere: the second write simply wins. That is the failure mode this
    exists to close, which is why the three recorders below use it rather than
    calling :func:`read_task_state` and then :func:`write_task_state`.
    """
    path = state_path_for(config, task.scope, workspace)
    with keyed_lock(_lock_key(path)):
        yield path, _read_record(path, task)


def _stamp(now: datetime | None) -> str:
    """The ISO-8601 UTC stamp a record carries.

    Taken from the caller so a test (and a replay) can say when a decision was
    made; the alternative, a clock read inside the writer, makes the record's
    own bytes depend on when it was written.
    """
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _carried(previous: TaskState | None) -> dict[str, Any]:
    """The fields a new lifecycle keeps from the record it replaces.

    A dismissal must not erase the chat the task was in, and a completion must
    not erase the fingerprint of the attempt it follows, so the three identity
    fields are always present and default to empty rather than being splatted
    conditionally. ``evidence`` is deliberately *not* carried: it is the
    reasoning behind the lifecycle being written, so a new lifecycle's evidence
    replaces the old one instead of accumulating into a record nobody can read.
    Only a *reopen* clears the fingerprint, because that is the one decision the
    operator is explicitly reversing.
    """
    if previous is None:
        return {"attempted_fingerprint": "", "chat_id": "", "prompt_digest": ""}
    return {
        "attempted_fingerprint": previous.attempted_fingerprint,
        "chat_id": previous.chat_id,
        "prompt_digest": previous.prompt_digest,
    }


def record_dismissal(
    task: UpdateTask,
    *,
    config: Any,
    workspace: str = "",
    now: datetime | None = None,
    reason: str = "",
) -> TaskState:
    """Record that the operator declined this task at this revision.

    The one place a lifecycle is written without a proof behind it, because a
    dismissal *is* the operator's decision and needs none. It suppresses the
    offer at this revision only; :func:`reopen_task` reverses it. The record it
    replaces keeps its identity fields (the chat the task was in, the attempt
    fingerprint, the prompt digest) and loses its evidence, which belonged to
    the lifecycle that is being replaced.

    The read of what it replaces and the write that replaces it happen in one
    critical section, so a chat id or attempt fingerprint recorded concurrently is
    carried forward rather than overwritten.
    """
    with _record_lock(task, config, workspace) as (path, previous):
        evidence: dict[str, Any] = {}
        if reason.strip():
            evidence["dismiss_reason"] = reason.strip()
        state = TaskState(
            task_id=task.id,
            revision=task.revision,
            scope=task.scope,
            lifecycle="dismissed",
            updated_at=_stamp(now),
            evidence=evidence,
            **_carried(previous),
        )
        _write_record(path, state)
    return state


def reopen_task(
    task: UpdateTask,
    *,
    config: Any,
    workspace: str = "",
    now: datetime | None = None,
) -> TaskState:
    """Put a dismissed task back on offer, and return the record that stands.

    Only ``dismissed`` is reopened, and only at this revision: the other
    lifecycles are an attempt in flight or a verdict already reached, and
    overriding either would be a decision nobody asked this function to make. So
    there are two no-write cases, and both return the record that stands rather
    than inventing one:

    * **nothing recorded** — there is no dismissal to reverse, and an absent
      record already means "offered" (``TaskStatus.state is None``). Writing an
      empty ``offered`` record would add a file the reader has to treat
      specially, for no information.
    * **a lifecycle that is not a dismissal** — returned unchanged, so reopening
      an offered task rewrites nothing.

    The attempt fingerprint is cleared, because that is what the dismissal was
    about: "not against this attempt" is not a statement the operator made about
    the next one.

    The read and the write share one critical section, so a fingerprint recorded
    concurrently is not silently dropped on the way out.
    """
    with _record_lock(task, config, workspace) as (path, previous):
        if previous is None:
            return TaskState(
                task_id=task.id,
                revision=task.revision,
                scope=task.scope,
                lifecycle="offered",
                updated_at=_stamp(now),
            )
        if previous.lifecycle != "dismissed":
            return previous
        carried = _carried(previous)
        carried["attempted_fingerprint"] = ""
        state = TaskState(
            task_id=task.id,
            revision=task.revision,
            scope=task.scope,
            lifecycle="offered",
            updated_at=_stamp(now),
            evidence={},
            **carried,
        )
        _write_record(path, state)
    return state


def record_completion(
    task: UpdateTask,
    *,
    config: Any,
    workspace: str = "",
    now: datetime | None = None,
    only_from: frozenset[str] | None = None,
) -> TaskState | None:
    """Record this task as done, if and only if its registered check proves it.

    Returns the written record, or ``None`` when nothing was written: the check
    name has no implementation, the check raised, it returned something other
    than a :class:`Detection`, it said the postcondition does not hold yet, or
    ``only_from`` was given and the record on disk had moved on. That is the whole
    reason this function exists rather than a caller writing
    ``lifecycle="completed"``: a task is done when a *named, registered,
    deterministic* check says so, evaluated apart from any chat the operator
    started. Opening a chat is not completion, and neither is silence.

    ``only_from`` is the guard :func:`_settle` needs and no other caller does.
    The check runs outside the write lock on purpose — it is somebody else's
    code and may be slow, and holding a scope's lock across it would serialise
    every other writer in that scope behind a probe — so the lifecycle this
    function started from may be minutes stale by the time it takes the lock. An
    operator who dismisses or reopens the task while a check is running must win,
    and without this the lock would serialise the two *writes* while still losing
    the *decision*: ``completed`` would go over the top of ``dismissed`` with no
    error anywhere, because the lock did its job and the read behind it did not.

    So when ``only_from`` is given the lifecycle is re-read **under** the lock and
    the write happens only if that read still shows one of those lifecycles. The
    decision that wins is the one on disk at the moment of the write, not the one
    the caller saw before it started waiting. ``None`` (a record) is refused too:
    an attempt this install cannot see is not one whose verdict may be recorded,
    and an unreadable scope must not be settled into.

    Left as ``None`` by default, which is unconditional and preserves this
    function's original contract for a caller that *knows* the lifecycle is
    settleable — a test pinning ``_carried``, or a future surface that has just
    written the attempt itself. A caller reasoning from a state it read earlier
    must pass the lifecycles it read.
    """
    check = COMPLETION_FUNCTIONS.get(task.completion_check)
    if check is None:
        logger.info(
            "update task %s@%s: no implementation for completion check %r, so "
            "nothing is recorded",
            task.id,
            task.revision,
            task.completion_check,
        )
        return None
    try:
        outcome = check(
            config=config,
            workspace=workspace,
            today=date.today() if now is None else now.date(),
        )
    except Exception as exc:  # noqa: BLE001 — a broken check must not raise here
        logger.warning(
            "update task %s@%s: completion check %s failed: %s",
            task.id,
            task.revision,
            task.completion_check,
            exc,
        )
        return None
    if not isinstance(outcome, Detection) or not outcome.applicable:
        return None
    # The check itself ran outside the lock: it is somebody else's code and may
    # be slow, and holding a scope's write lock across it would serialise every
    # other writer in that scope behind a probe. The read of what it replaces and
    # the write that replaces it are still one critical section.
    with _record_lock(task, config, workspace) as (path, previous):
        if only_from is not None and (
            previous is None or previous.lifecycle not in only_from
        ):
            # Someone decided while the check ran. That decision is the truth
            # about this task now, and it is not ours to overwrite — the whole
            # point of a settlement is that it follows the operator rather than
            # racing them.
            logger.info(
                "update task %s@%s: not settling, the record moved to %s while "
                "the completion check ran",
                task.id,
                task.revision,
                "no readable record" if previous is None else previous.lifecycle,
            )
            return None
        state = TaskState(
            task_id=task.id,
            revision=task.revision,
            scope=task.scope,
            lifecycle="completed",
            updated_at=_stamp(now),
            evidence=_portable_evidence(outcome.evidence),
            **_carried(previous),
        )
        _write_record(path, state)
    return state


# ── Applicability ───────────────────────────────────────────────────────────


def _canonical(value: Any) -> str:
    """A stable string for anything, so a fingerprint can always be computed."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True
        )
    except (TypeError, ValueError):
        return repr(value)


def fingerprint(task: UpdateTask, status: str, evidence: Mapping[str, Any]) -> str:
    """The digest identifying one task's exact situation.

    Over the task's identity, its detector name, the status and the canonical
    evidence — the four things that decide the answer. Rewording the same
    evidence does not change it; a different count does.
    """
    blob = _canonical(
        {
            "task": task_key(task.id, task.revision),
            "detector": task.detector,
            "status": status,
            "evidence": evidence,
        }
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def _result(
    task: UpdateTask, status: str, evidence: Mapping[str, Any]
) -> ApplicabilityResult:
    stored = dict(evidence)
    return ApplicabilityResult(
        status=status,
        evidence=stored,
        fingerprint=fingerprint(task, status, stored),
        # No injected clock: this is the moment the answer was computed, and the
        # only caller that could supply one (``_evaluate_off_loop``) has a
        # freshness clock rather than a wall clock to offer. Reading it here is
        # the point — a stamp taken from the caller's clock would be the age of
        # the window, which is not a date.
        checked_at=_stamp(now=None),
    )


def apply_detector(
    task: UpdateTask,
    *,
    config: Any,
    workspace: str = "",
    today: date | None = None,
) -> ApplicabilityResult:
    """Decide whether ``task`` applies, by resolving its detector name.

    Three ways this is not a claim, and all three answer ``unknown``:

    * the name has no implementation in ``DETECTOR_FUNCTIONS`` (a row shipped
      ahead of its code, or a name the catalog validated against a registry this
      module does not implement);
    * the implementation raised — a detector is code somebody will get wrong, and
      a Home render must not go down with it;
    * it returned something that is not a :class:`Detection`.

    ``today`` is passed through rather than read here, so a detector's answer is
    reproducible: a detector that depends on the date must be able to be asked
    what it would have said on another day.

    Total by construction, and never ``applicable`` by accident: only a detector
    that ran and returned ``applicable=True`` produces that status.
    """
    name = task.detector
    detector = DETECTOR_FUNCTIONS.get(name)
    if detector is None:
        return _result(
            task, UNKNOWN, {"reason": "detector_not_implemented", "detector": name}
        )
    try:
        outcome = detector(
            config=config, workspace=workspace, today=today or date.today()
        )
    except Exception as exc:  # noqa: BLE001 — one bad detector must not crash a render
        logger.warning(
            "update task %s@%s: detector %s failed: %s",
            task.id,
            task.revision,
            name,
            exc,
        )
        return _result(
            task,
            UNKNOWN,
            {
                "reason": "detector_failed",
                "detector": name,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
    if not isinstance(outcome, Detection):
        return _result(
            task,
            UNKNOWN,
            {
                "reason": "detector_returned_no_detection",
                "detector": name,
                "returned": type(outcome).__name__,
            },
        )
    status = APPLICABLE if outcome.applicable else NOT_APPLICABLE
    return _result(task, status, {"detector": name, **outcome.evidence})


# ── The cached check ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """One task's answer, and the situation and the moment it was computed in.

    One entry per ``(scope, "id@revision")``, not per scope. That is the whole
    point: the freshness window is a claim about an *answer*, so it has to travel
    with the answer it describes. An entry also outlives the eligible set that
    produced it — a task a downgrade hides is not evaluated, so its entry is left
    alone rather than replaced, and upgrading again reuses it while it is still
    inside its own window.

    ``computed_at`` is the freshness clock (``time.monotonic``) and the *only*
    clock this module compares ages in: monotonic because the wall clock can jump,
    and a window is an age rather than a date. The wall-clock moment the answer
    was computed travels separately on the result (``ApplicabilityResult
    .checked_at``) because that is a thing to *show* an operator, not a thing to
    subtract.
    """

    token: str
    computed_at: float
    result: ApplicabilityResult


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def clear_applicability_cache() -> None:
    """Drop every cached applicability. Test isolation hook.

    The cache is process-wide and keyed by scope and task, so a test that
    evaluates a fixture workspace could otherwise be served the answer a
    previous test computed for the same scope key. Named like
    ``async_reads.reset_vault_read_executor`` because it is the same kind of
    hook: a module-level cache with a lifetime the process does not own.
    """
    with _CACHE_LOCK:
        _CACHE.clear()


def _is_cacheable(result: ApplicabilityResult) -> bool:
    """True when this answer may be reused inside its window.

    Judged on the reason the answer carries, because that is the only place the
    distinction between "the workspace looks like this" and "I could not find out"
    is written down.
    """
    return result.evidence.get("reason") not in UNCACHEABLE_REASONS


def _scope_key(config: Any, task: UpdateTask, workspace: str) -> str:
    """The identity a cached answer belongs to: one scope of one install.

    The state file's path, which is the workspace's own vault for a workspace
    task and the runtime directory for an install task. Nothing is derived from
    the task id, so a catalog that adds, reorders or drops a task does not
    invalidate the answers of the others.
    """
    return f"{task.scope}:{state_path_for(config, task.scope, workspace)}"


def _read_key(config: Any, task: UpdateTask, workspace: str) -> str:
    """The cache key for one task's answer: its scope, then its exact revision."""
    return f"{_scope_key(config, task, workspace)}\0{task_key(task.id, task.revision)}"


def _cached_results(
    tasks: tuple[UpdateTask, ...],
    config: Any,
    workspace: str,
    token: str,
    instant: float,
) -> dict[str, ApplicabilityResult]:
    """The answers that are still inside their own window, keyed by read key.

    Both halves of the window are checked per answer, not per scope: this used to
    hold one ``(token, computed_at)`` for every task in a scope and re-stamp the
    whole set whenever any task was recomputed, so a task evaluated early in a
    window was served as fresh until the *last* evaluation's window closed — and a
    task whose answer was never recomputed was served under a change token it was
    not computed for. One entry per ``(scope, task)`` is the only arrangement in
    which "this answer, for this situation, inside this age" is a statement about
    the answer.

    Token equality is strict in both directions: a caller that supplies no token
    gets a purely time-based window, and a caller that supplies one never reuses
    an answer computed for a different situation (or for no situation at all). A
    negative age — an injected or stepped clock — is treated as stale, because a
    window that has not happened yet is not a window.
    """
    out: dict[str, ApplicabilityResult] = {}
    with _CACHE_LOCK:
        for task in tasks:
            entry = _CACHE.get(_read_key(config, task, workspace))
            if entry is None or entry.token != token:
                continue
            age = instant - entry.computed_at
            if not 0 <= age < APPLICABILITY_TTL_S:
                continue
            out[_read_key(config, task, workspace)] = entry.result
    return out


def _store_results(
    fresh: Mapping[str, ApplicabilityResult], token: str, instant: float
) -> None:
    """Publish freshly computed answers, one entry per ``(scope, task)``.

    Nothing is merged and nothing is re-stamped: an entry written here describes
    exactly the answers computed in this call, and an entry left alone keeps the
    token and the age it was computed with. That is also what keeps a task that
    has temporarily dropped out of the eligible set (the downgrade case) cached
    for the rest of *its own* window rather than being re-run on the way back up.
    """
    with _CACHE_LOCK:
        for key, result in fresh.items():
            _CACHE[key] = _CacheEntry(
                token=token, computed_at=instant, result=result
            )


def _evaluate_off_loop(
    tasks: tuple[UpdateTask, ...],
    *,
    config: Any,
    workspace: str,
    cached: Mapping[str, ApplicabilityResult],
    token: str,
    today: date | None,
    instant: float,
) -> list[TaskStatus]:
    """The whole check, off the event loop: state first, then what is missing.

    State is read *first* so an unreadable document can stop the work instead of
    qualifying it: a detector's answer is not one this install may report while
    it cannot say what the operator already decided. The state read is one small
    JSON document per scope and happens on every call — a dismissal the operator
    just made must not wait out a TTL. The detectors, which are the part that can
    be expensive, run only for tasks with no answer inside their window.

    The unreadable check is deliberately ahead of the cache lookup, and an answer
    it produces is never stored. The other order reads as a harmless
    short-circuit and is not one: a file that becomes unreadable inside a window
    (a half-finished sync, a hand edit) would be answered from the cache with
    ``state=None``, so a task the operator dismissed would come back as
    ``offered`` on the strength of an answer the install can no longer stand
    behind. Reading the record on every call means the record is what decides
    whether an answer may be used at all.

    A last pass settles whatever the detectors found, before the statuses are
    built, so the row a caller renders is the settled one rather than the state
    this call replaced. The statuses are assembled from the ``states`` mapping,
    so writing the settled record back into it is all that is needed for one
    call to both answer ``applicable`` and report ``completed`` — the honest
    pair for a workspace where the review retained rows the detector still
    counts, and the pair that takes the card off Home.
    """
    states, unreadable = _load_states(tasks, config, workspace)
    computed: dict[str, ApplicabilityResult] = {}
    results: dict[tuple[str, int], ApplicabilityResult] = {}
    fresh: set[tuple[str, int]] = set()
    for task in tasks:
        identity = (task.id, task.revision)
        key = _read_key(config, task, workspace)
        if _scope_key(config, task, workspace) in unreadable:
            # The file's *name* only: a state file's own name is the one place
            # a path-shaped string is safe to record, and it is what tells an
            # operator which file to look at.
            results[identity] = _result(
                task,
                UNKNOWN,
                {
                    "reason": "state_unreadable",
                    "file": state_path_for(config, task.scope, workspace).name,
                },
            )
            continue
        hit = cached.get(key)
        if hit is not None:
            results[identity] = hit
            continue
        answer = apply_detector(task, config=config, workspace=workspace, today=today)
        results[identity] = answer
        # Freshly computed, whether or not the answer is one that may be cached:
        # a detector that raised is retried inside the window by design, and the
        # completion check is not the detector's business — a task whose evidence
        # is on disk can be settled even on the turn where its detector is
        # unwell.
        fresh.add(identity)
        if _is_cacheable(answer):
            computed[key] = answer
    if computed:
        _store_results(computed, token, instant)
    _settle(tasks, states, config, workspace, fresh)
    return [
        TaskStatus(
            task=task,
            applicability=results[(task.id, task.revision)],
            state=states[(task.id, task.revision)],
        )
        for task in tasks
    ]


def _settle(
    tasks: tuple[UpdateTask, ...],
    states: dict[tuple[str, int], TaskState | None],
    config: Any,
    workspace: str,
    fresh: set[tuple[str, int]],
) -> None:
    """Ask each started task's own check whether its work is done, in place.

    ``fresh`` is the set of ``(id, revision)`` identities this call computed
    rather than served from the window, so a check runs at most once per task per
    window — the same bound the detector has, and deliberately the same window
    rather than a second timer.

    Only a record in :data:`SETTLING_LIFECYCLES` is asked, and only about tasks
    whose state this install may act on: the ``states`` mapping holds ``None`` for
    an unreadable scope, and ``None`` is not a lifecycle, so a task whose record
    cannot be read is left alone rather than settled on the strength of evidence
    the record contradicts.

    That read is at the top of the evaluation and the check may be slow, so the
    lifecycle it was chosen from is stale by the time the check answers. The
    authority is still :func:`record_completion` — it resolves the row's
    registered check and writes nothing for one that is unregistered, raising,
    rude or unsatisfied — but ``only_from=SETTLING_LIFECYCLES`` makes it
    re-check that lifecycle under the write lock, so an operator who dismissed or
    reopened the task mid-check wins instead of being overwritten.

    Two refusals leave the record alone, and both mean the record on disk is no
    longer what this call read: a dismissal decided mid-check, and a write that
    could not land (:class:`UpdateTaskStateError`, :class:`OSError` — a document
    that went unreadable, a full disk). In both cases the record is re-read and
    reported as it now stands. Reporting the stale copy would put a card on Home
    offering Start for a task the operator had just dismissed, one poll after the
    poll that decided it — so the re-read is not tidiness, it is the answer.

    A write that fails is logged and swallowed. #788's rule is that an unfinished
    task is reported as unfinished rather than as an error, and this runs on a
    Home listing: one task whose state file cannot be written must not take the
    whole strip down, and must not be reported as done either.
    """
    for task in tasks:
        identity = (task.id, task.revision)
        if identity not in fresh:
            continue
        state = states.get(identity)
        if state is None or state.lifecycle not in SETTLING_LIFECYCLES:
            continue
        try:
            record_completion(
                task,
                config=config,
                workspace=workspace,
                only_from=SETTLING_LIFECYCLES,
            )
        except (UpdateTaskStateError, OSError) as exc:
            logger.warning(
                "update task %s@%s: the completion check said the work is done but "
                "the record could not be written (%s); it stays %s until a later "
                "listing settles it",
                task.id,
                task.revision,
                exc,
                state.lifecycle,
            )
        # Re-read whether the call settled the task or refused it: a settlement
        # changed the record, and a refusal — a decision that arrived mid-check,
        # or a write that could not land — means it changed underneath this call.
        # Either way the file is the account of itself this call reports, never
        # the copy read before a check that may have raced it.
        states[identity] = read_task_state(task, config=config, workspace=workspace)


async def evaluate(
    config: Any,
    *,
    workspace: str = "",
    installed_version: str,
    catalog: TaskCatalog | None = None,
    change_token: str = "",
    today: date | None = None,
    now: float | None = None,
) -> list[TaskStatus]:
    """Every task this install supports, with its answer and its state.

    Eligibility is the catalog's own ``eligible(installed_version)`` — a cumulative
    catalog stays cumulative, and a version that does not parse supports nothing.
    A task this version cannot support is simply not in the list; nothing here
    deletes its record, so a downgrade hides the task and upgrading brings it
    back with its history.

    ``catalog`` defaults to the packaged one, which is what a caller with no
    better answer wants; a caller that has already loaded it passes it in rather
    than reading package data twice.

    ``change_token`` is the caller's own answer to "has this workspace changed?".
    A different token invalidates the cached answers immediately rather than
    waiting out ``APPLICABILITY_TTL_S``; the same token inside that window reuses
    them, which is the whole reason this does not walk the vault on every call.
    The token is compared, never stored, and never interpreted.

    ``now`` is the freshness clock (``time.monotonic`` by default) and ``today``
    is the date handed to detectors. Both are injectable so a test can ask about a
    window and a day without waiting for either.

    The work runs through ``ciao.async_reads.run_read``, so it is bounded,
    coalesced by scope/version/token, and never blocks the event loop: a detector
    that walks a large vault must not stall the heartbeats behind it. The
    coalescing key is deliberately the same for a cached and an uncached call —
    two callers of one scope sharing a read share whichever shape of it is
    running, and both are correct answers.

    This is a read of the workspace and a write to one task's own record, and
    only that write: a task whose attempt has produced the evidence its
    registered completion check looks for becomes ``completed`` here, through
    :func:`_settle`. It creates no chat, sends no prompt, starts no model turn,
    moves no note, and can only ever write the lifecycle
    :func:`record_completion` is documented to write — an unregistered, raising
    or unsatisfied check records nothing at all. A settlement that loses a race
    with an operator's decision, or whose write cannot land, is logged and
    skipped rather than raised, so a state file that goes unwritable costs one
    task its completion and never the listing. The write rides the same window as
    the detector, so it becomes visible within ``APPLICABILITY_TTL_S`` of the
    evidence landing rather than immediately; the state file itself is still read
    on every call, so a settled record shows up on the very next listing.
    """
    loaded = catalog if catalog is not None else load_catalog()
    tasks = loaded.eligible(installed_version)
    if not tasks:
        return []
    instant = time.monotonic() if now is None else now
    cached = _cached_results(tasks, config, workspace, change_token, instant)
    # The install's runtime directory is in the key because it is the one part
    # of a scope's identity a workspace name and a version cannot supply: two
    # installs in one process (a test, a dev checkout beside a real engine) must
    # not share a read.
    key = (
        f"update-tasks:{config.state_path.parent}:"
        f"{workspace or '-'}:{installed_version}:{change_token}"
    )
    return await run_read(
        key,
        lambda: _evaluate_off_loop(
            tasks,
            config=config,
            workspace=workspace,
            cached=cached,
            token=change_token,
            today=today,
            instant=instant,
        ),
    )


def offered_tasks(statuses: list[TaskStatus]) -> list[TaskStatus]:
    """The subset a caller should put in front of the operator.

    ``applicable`` and not suppressed at this revision. ``unknown`` and
    ``not_applicable`` are both absent, which is the honest answer for a
    condition this install cannot substantiate — see the module docstring.
    """
    return [status for status in statuses if status.offered]
