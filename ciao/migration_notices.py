"""The migration notices, each probed once and stated once.

Why this module exists
----------------------
Three surfaces answer "what has an upgrade left for this install?": the Home strip
(``operator_actions.detect_actions``, polled every 60s and on window focus), the
``upgrade_notices`` section of the OS audit (a diagnostic report, never red), and
since #833 the "After this update" catalog in ``ciao/update_tasks.py``, whose card
is an *offer* the operator may dismiss where the audit is a report they may not
silence. The first two of them used to hold their own copy of two conditions
each, and the copies had drifted in ways only one of them could see:

* **vault location.** One predicate, two implementations. The audit's remedy also
  taught the manual path — "identify which files are vault content, make a
  backup, move the approved content, atomically update the active workspace
  registry" — which is the path the engine refuses, that
  ``docs/VAULT_MIGRATION_PROMPT.md`` no longer teaches since #815, and that
  ``ciao vault-relocate`` exists to replace. Two surfaces, two wordings, and one
  of them wrong.
* **unmigrated links.** Two different *applicability* rules. Home asked for an
  adopted vault (``vault_mode == "existing"``) with no completed link-migration
  receipt; the audit asked only for the receipt. Making the two agree by
  adopting either rule loses something real — Home's version never reports a
  vault with no wikilinks in it, and the audit's version points at a migration
  on every scratch install that has nothing to convert. The disagreement is not
  in the *condition*; it is in who is allowed to establish it. So the condition
  is one function and the cost is a named argument, and the fix for a surface
  that cannot afford the walk is to give it a bounded off-loop one rather than a
  cheaper definition.

The condition, and who may establish it
----------------------------------------
The wikilink notice applies exactly when two facts hold: no **completed**
link-migration receipt, and an actual wikilink still in the vault. Both halves
are the same question for both surfaces, and neither surface gets to decide that
half of it differently — which is what the mode gate was. A ``scratch`` vault is
created conformant and so is clean, but a vault this engine created is also the
one an operator can hand a wikilink, and reporting that is the audit's whole
purpose. So the mode is gone from the probe, and the second half is established
by a walk:

* :func:`resolve_links` runs it. The audit calls it directly, because the audit
  is already a full-install pass that reads every note, and because a report that
  reused a cached verdict could report a stale one. It publishes what it found.
* :func:`cached_links` only reads what a previous scan stored. Home calls it: the
  strip runs on every app open, every window focus and every 60s poll, and this
  is the one fact on it that costs a vault.
* :func:`start_links_scan` puts the walk on the bounded vault-read executor
  (:func:`ciao.async_reads.run_read`, so it is coalesced per install,
  admission-capped, and never on the event loop) and starts it **detached** from
  the Home route, which answers the poll from whatever the last scan stored. That
  is the same trade ``_cached_update_hint`` makes: a card appears a poll or two
  after the first scan rather than on the first render. It also owns the task's
  whole life — one in flight at a time, its failure observed rather than left for
  the garbage collector, and cancellation at shutdown through
  :func:`shutdown_links_scan`, which ``ciao/main.py`` registers beside the other
  teardown callbacks.

A stored answer is one of three states, and the difference matters. ``found`` is a
wikilink a walk located; ``clean`` is a walk that finished and found nothing; and
``failed`` is a walk or a receipt read that did not finish. Only ``found`` is ever
a finding, and ``failed`` is never cached as ``clean`` — a vault that could not be
read is an unknown, and an unknown reporting itself as clean would be the notice
quietly lying. ``failed`` is still worth caching, because the alternative is a
broken vault re-walking and re-logging on every 60s poll; the window is how long
this engine waits before it looks again.

:data:`LINKS_SCAN_TTL_S` is how long one answer may be reused. It is a named
constant, not a setting and not an env var, and it is deliberately the same
window ``update_tasks.APPLICABILITY_TTL_S`` uses for its detectors: one clock for
how fast this engine looks at the world, rather than a second one per surface.
Re-establishing a positive answer is cheap (the walk stops at the first hit);
re-establishing a clean one costs the whole vault, which is why the window is
minutes and not seconds, and why the audit never waits for it.

Nothing in this module opens a file under a vault on a Home poll.
``tests/test_migration_notices.py`` pins that as a count of filesystem accesses at
1 note and at 400, not as a duration.

Audit truth, Home honesty
-------------------------
Sharing a probe must not turn the audit into a mirror of the Home strip, and
sharing a cache must not turn the audit into a slower copy of Home. The audit
resolves the verdict itself and reports it; Home reports the last established
one. No Home-side suppression is an input to any function here, so the notice
#833 made an optional, dismissible catalog card still cannot silence the audit:
``rehomed_people_finding`` reads the registry and the receipt and nothing else,
and a lifecycle recorded in ``<runtime>/update-tasks.json`` is not one of its
inputs. That is what lets one condition be shared by an *offer* and a *report*
without the offer being able to erase the report.

The vault location is the same asymmetry with one fewer variable: since this
slice the Home *tile* is gone and the condition has an offer (a workspace-scoped
``vault-relocate`` catalog task) beside the report, and both read
``vault_location_findings``, so they cannot disagree about which workspaces are
misplaced — only about what the operator did about it. See
:func:`completed_relocation` for why the reversible remedy makes this the one
notice whose completion is a receipt *and* the current state, and why that is the
property that lets a suppressed task come back.

One notice has two questions, and they are not the same question
----------------------------------------------------------------
``unrehomed_people`` is the only notice here a surface answers differently on
purpose, and #833's review is why. There are two questions:

* **Is there anything here an upgrade left undone?** That is a *diagnostic*, and
  :func:`rehomed_people_finding` answers it with a **receipt check**: more than
  one registered workspace and no completed ``vault-rehome`` receipt. It opens no
  file under a vault, which is what makes it safe for a poll and for a report, and
  it cannot tell you anything is actually misfiled — hence the hedge in its own
  wording. It stays broad and advisory on purpose: a migration nobody ever needed
  is still worth mentioning once, in a place that never turns the report red.
* **Is there something to actually do?** That is the catalog task's question, and
  it is much the harder one. #833's first pass answered it with the receipt check
  too, which offered the card on every fresh conforming install — no receipt, two
  workspaces, no legacy data at all — the false positive #800's table rules out by
  name. :func:`rehome_legacy_candidates` answers it instead, from
  ``vault_rehome.plan_rehome``: four cheap gates that each *prove* a mechanical
  candidate is unreachable, and only then one plan walk over the shared vault.
  Only a tag-obvious cross-workspace move counts; untagged person notes are
  ordinary on a fresh install and are never treated as evidence of legacy damage.

So the asymmetry is deliberate and bounded in one direction only:

    task offered  =>  audit reports
    audit silent  =>  task not offered

A completed receipt silences both (it is gate 1, and the notice's own term). The
task is otherwise **narrower** than the notice: an install the audit describes as
"person notes may be filed in the wrong workspace" gets no card until the plan
says there is a move. Nothing widens the task, because #833's review is the
evidence for what widening it costs.

And the walk is paid for by the layer, not by the probe: it runs from
``update_tasks.evaluate``, off the event loop through ``ciao.async_reads.run_read``,
coalesced per install, admission-capped with every other vault read, and at most
once per ``APPLICABILITY_TTL_S``. ``operator_actions`` and ``os_audit`` never call
it. Any new surface that cannot afford a vault read wants
:func:`rehomed_people_finding`, and the pinned claim that a **Home poll** opens no
file under the vault still holds — the module now contains a probe that walks, and
the test that counts accesses proves nothing on a poll reaches it.

The receipt is read through :func:`completed_rehome` on every side, deliberately.
A detector that says "no completed receipt" and a completion check that says "a
completed receipt" are the same question asked twice, and two readers of one file
are two rules — the drift this module exists to remove. ``vault_rehome
.read_receipt`` is the canonical completed-only reader (a ``partial`` receipt is
incomplete, a receipt predating the ``status`` field counts as complete), and all
three callers go through it here rather than each reaching for the file.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.async_reads import run_read

logger = logging.getLogger(__name__)

#: The audit's notice type for a workspace vault kept outside its standard folder.
VAULT_LOCATION_NOTICE = "vault_outside_vault_root"

#: The audit's notice type for a vault still written in the retired wikilink dialect.
UNMIGRATED_LINKS_NOTICE = "unmigrated_vault_links"


#: The audit's notice type for person notes that may still need re-homing. The
#: same condition #833's ``unrehomed-people`` update task offers on Home, so the
#: string is the join between the report and the card; it is one spelling because
#: two surfaces that have to agree on which notice this is cannot each invent it.
UNREHOMED_PEOPLE_NOTICE = "unrehomed_people"

# -- vault relocation, the task half -----------------------------------------
#
# The vault-location notice is the one #800's evidence table got wrong, and the
# correction matters more than the promotion: the table said the remedy wrote no
# receipt, which would have made this shape 3 and kept it a tile. It writes one —
# per workspace, `status: relocated` on success, and removed again by `--undo`
# (see `vault_relocate.read_receipt`). So the notice is promotable, and the shape
# is a *fourth* one rather than either of the three the table names: a real
# receipt, but on a **reversible** remedy.
#
# That is the property #833's task cannot have and this one needs. `evaluate`
# settles a started task once and then suppresses the offer for the whole
# revision, so a task whose condition can come *back* needs evidence that goes
# away when the work is undone — otherwise `--undo` would leave the card hidden
# while the vault sat outside its folder again, with nothing on Home saying so.
# `vault-relocate`'s receipt is exactly that: undo deletes it, so a re-misplaced
# vault is applicable work again rather than a task already finished. The audit
# notice covers the same window on its own, and stays independent throughout.
#
# The asymmetry is the same one-directional shape as the re-home task, and for a
# sharper reason — here the task and the notice ask the *same* question, so they
# cannot disagree about it, and only the response differs:
#
#     task offered  =>  audit reports
#     dismissed/relocated  =>  card gone; the audit still reports any new mismatch
#
# No update-task record is an input to anything in this module, which is what
# keeps the two apart: the card is an offer the operator may decline, the report
# is not theirs to silence.

#: The workspace's vault is registered outside its standard folder and is there,
#: so there is something to relocate. The only reason that offers the card; the
#: others exist so a task that is *not* offered reads as a reason rather than as
#: silence.
RELOCATION_MISPLACED = "misplaced_vault"

#: The registry and the layout already agree. Not applicable, and the ordinary
#: answer on a healthy install — a detector that cannot reach zero here is a
#: detector that offers work for ever.
RELOCATION_STANDARD = "standard_location"

#: The registered path is not a directory. A **different notice**
#: (`workspace-root-missing` on Home, a setup finding in the audit), and there is
#: no misplaced vault to relocate: offering a move for a path that is not there is
#: the unactionable card operators learn to ignore.
RELOCATION_VAULT_MISSING = "vault_missing"

#: A **completed** relocation is recorded for this workspace, and the workspace
#: no longer sits outside its folder. Only a `status: "relocated"` receipt counts:
#: a `refused` one means a run happened and moved nothing, and a receipt this
#: install cannot parse proves nothing in either direction.
RELOCATION_DONE = "relocated"

#: No completed receipt. The task has not been done, whatever any other receipt
#: shape says — including a `refused` one, which is what most retries write.
RELOCATION_NOT_DONE = "no_completed_relocation_receipt"

#: A completed receipt exists but the workspace is **mismatched again**. Reachable
#: by hand — a registry edit, a restore from an old copy, a partial undo — and it
#: is the exact state a check reading only the receipt would report as finished.
#: It cannot happen through `--undo`, which removes the receipt; it can happen
#: through anything that does not go through the receipt.
RELOCATION_MISMATCHED = "relocated_but_still_mismatched"

#: How long an established wikilink verdict may be reused before it is recomputed.
#:
#: The same window `update_tasks.APPLICABILITY_TTL_S` gives its detectors, and
#: for the same reason: it is how long this engine is willing to go on an answer
#: about somebody's own notes, not a decision an operator has asked to make. One
#: walk per install per window, on the bounded read executor, never on the event
#: loop. The audit does not wait for it — a report resolves the verdict itself.
LINKS_SCAN_TTL_S = 300.0


# -- vault location ----------------------------------------------------------


@dataclass(frozen=True)
class VaultLocationFinding:
    """One workspace whose registered vault is not where the layout says it is.

    The registry *is* the layout, so this is a comparison of two resolved paths
    rather than a scan, and the paths themselves are the whole of the evidence.
    Every sentence a surface shows about the condition is derived here, so the
    two cannot describe the same mismatch differently.
    """

    workspace: str
    actual: Path
    standard: Path

    @property
    def title(self) -> str:
        return f"The {self.workspace} vault is not in its standard folder"

    @property
    def detail(self) -> str:
        return (
            f"Workspace '{self.workspace}' keeps its vault at the nonstandard "
            f"location {self.actual}; its standard location is {self.standard}."
        )

    @property
    def remedy(self) -> str:
        """The managed command, the way back, and the truth about refusing.

        `vault-relocate` has an apply/undo cycle and updates the registry itself,
        so a remedy that describes moving the folder and hand-editing the
        registry teaches a path the engine refuses and the operator cannot
        reproduce. `--undo` restores the exact previous location and a restart is
        needed either way, so the way back and the restart belong to the same
        sentence as the way forward.

        The refusals belong here rather than in a chat prompt, because both
        surfaces have to say them and only one of them opens a chat. What this
        must NOT claim is that every refusal hands the operator a way forward:
        most of them only report what would not be done. So the sentence names
        the shapes, points at the preview as the place that says which one
        applies, and attributes a route only to the refusals that really carry
        one — the install-root and external-vault shapes, which
        ``vault_relocate`` finishes by hand, and a destination nested under the
        vault, which needs the registry repointed first. Whether this particular
        mismatch is one of those is ``vault_relocate.plan``'s answer and not this
        notice's to pre-empt.
        """
        name = self.workspace
        return (
            f"Preview with `ciao vault-relocate {name}`, then apply with "
            f"`ciao vault-relocate {name} --apply`. It moves this workspace's own "
            "content into the standard folder, updates the registry, and can be "
            f"reversed exactly with `ciao vault-relocate {name} --undo`. "
            "Ciaobot needs a restart (Settings -> Restart) before the new location "
            "takes effect everywhere, including in an open chat. It refuses rather "
            "than guessing, and the preview says which shape applies: a symlink, a "
            "destination that is not empty, a vault root that is the install root "
            "itself, a shared root claimed by more than one workspace, a vault "
            "holding another workspace's root, a destination nested under the vault, "
            "uncommitted changes under the source, or a vault outside the install's "
            "git worktree. A few of those name the way forward — the install-root and "
            "outside-the-worktree shapes are finished by hand — and the rest report "
            "only what would not be done, so nothing is moved on a guess."
        )


def vault_location_findings(config: Any) -> list[VaultLocationFinding]:
    """Every registered workspace whose vault sits outside its standard folder.

    A config without a workspace registry exposes none of the three accessors
    this needs, and there is nothing for it to report. A registry entry that
    cannot be resolved is skipped rather than raised: this is a notice, and a
    broken registry must not take an audit or a Home render down with it.
    """
    resolver = getattr(config, "workspace_vault_root", None)
    standardizer = getattr(config, "canonical_workspace_vault_root", None)
    lister = getattr(config, "workspace_names", None)
    if not callable(resolver) or not callable(standardizer) or not callable(lister):
        return []
    findings: list[VaultLocationFinding] = []
    for name in lister():
        try:
            actual = Path(resolver(name)).resolve()
            standard = Path(standardizer(name)).resolve()
        except Exception:  # noqa: BLE001 — advisory; a bad registry must not fail
            continue
        # A root that does not exist is a different notice (`workspace-root-missing`
        # on Home, and a setup finding in the audit): there is no misplaced vault
        # to relocate, and offering a move for a path that is not there is the
        # unactionable tile operators learn to ignore.
        if actual == standard or not actual.is_dir():
            continue
        findings.append(
            VaultLocationFinding(workspace=str(name), actual=actual, standard=standard)
        )
    return findings


def completed_relocation(runtime_dir: Path, workspace: str) -> dict[str, Any] | None:
    """The receipt of a **completed** relocation for one workspace, or ``None``.

    One reader, for the same reason :func:`completed_rehome` is one reader: the
    question is asked in opposite directions by two surfaces — the catalog's
    completion check asks "is there one?" and a caller deciding whether the work
    is still outstanding asks "is there not one?" — and two callers reaching for
    ``vault-relocate-<workspace>.json`` with different ideas of what counts as
    done is how a task and a report come to disagree about the same workspace.

    The accessor is :func:`ciao.vault_relocate.read_receipt`, which gates on
    ``status == "relocated"``. Two properties of that gate are the whole reason
    this notice was promotable:

    * a ``refused`` receipt is **not** completion. Most of the shapes
      ``vault_relocate.plan`` refuses are permanent properties of an install, so
      a retry writes one every time and none of them moved a note.
    * ``--undo`` **deletes** the receipt, so an undone relocation reads as no
      relocation at all. The task lifecycle suppresses a completed task for the
      rest of its revision, so a check that could only see "a receipt once
      existed" would hide the card while the vault was back outside its folder.

    No tolerance for a receipt with no ``status`` field, unlike
    ``vault_rehome``. Every run this engine has written names its own status, so
    a file without one is not this engine's record of anything and reading it as
    proof would be reading an uninterpretable file as evidence.

    Returns the receipt rather than a boolean, because the completion check has
    to name the run it looked at in its evidence and a bare ``True`` cannot.
    """
    from ciao.vault_relocate import read_receipt

    return read_receipt(Path(runtime_dir), str(workspace))


def relocation_state(config: Any, workspace: str) -> str:
    """Where this workspace's vault sits *right now*, as one of the reasons above.

    The *current* half of the vault-location condition, resolved from the same two
    accessors :func:`vault_location_findings` reads, so the task's answer and the
    notice's are one predicate over one registry rather than two rules that can
    drift. It is a question about the present, which is why completion needs it
    **and** the receipt: the receipt says a run finished, and this says the layout
    still agrees. A relocated workspace whose registry was later hand-edited has
    the first and not the second.

    One workspace only, and that is the point rather than a convenience: a
    mismatch in a *different* workspace is a different task record in a different
    vault, so a workspace task must never read another workspace's answer as its
    own. ``vault_location_findings`` returns every workspace's; this asks about
    one.

    :func:`vault_location_findings` *skips* an entry it cannot resolve, because a
    broken registry must not take an audit or a Home render down with it. Here the
    same fault **raises**, and the difference is deliberate rather than
    accidental. Skipping answers "there is nothing to report", which is the right
    thing for a diagnostic that renders and the wrong thing for a card: the task
    would offer nothing on the strength of an entry it never read, and the
    operator would have no way to tell that from a clean workspace. ``update_tasks``
    turns a raise into ``unknown`` — no card offered, and the answer recorded as
    not known rather than as known-absent — which is the same shape the re-home
    task settled on for the same fault.
    """
    resolver = getattr(config, "workspace_vault_root", None)
    standardizer = getattr(config, "canonical_workspace_vault_root", None)
    if not callable(resolver) or not callable(standardizer):
        raise ValueError(
            "the vault-relocate task needs a config with a workspace registry"
        )
    try:
        actual = Path(resolver(workspace)).resolve()
        standard = Path(standardizer(workspace)).resolve()
    except Exception as exc:  # noqa: BLE001 — an unresolvable entry is an unknown
        raise ValueError(
            f"the workspace registry does not resolve {workspace!r} to a vault "
            f"root: {exc}"
        ) from exc
    if actual == standard:
        return RELOCATION_STANDARD
    # A root that is not there is a different notice (`workspace-root-missing` on
    # Home, a setup finding in the audit) and there is no misplaced vault to
    # relocate — the same skip `vault_location_findings` makes, named rather than
    # silent so a card-free workspace is readable as a reason.
    if not actual.is_dir():
        return RELOCATION_VAULT_MISSING
    return RELOCATION_MISPLACED


def relocation_mismatch(config: Any, workspace: str) -> bool:
    """Whether this workspace's vault is currently outside its standard folder.

    :func:`relocation_state` narrowed to the question the completion check asks,
    and it inherits that function's raise-on-unresolvable behaviour: a check that
    could not read the layout must not report the postcondition as holding.
    """
    return relocation_state(config, workspace) == RELOCATION_MISPLACED


# -- unmigrated links --------------------------------------------------------


@dataclass(frozen=True)
class LinksFinding:
    """A vault that still holds a wikilink, and no completed migration for it.

    ``example`` is the first note still holding one, relative to the vault root.
    It is never empty: this object exists only once a walk established that a
    wikilink is really there, which is the difference between this notice and the
    receipt's absence that used to stand in for it.
    """

    vault_root: Path
    example: str

    @property
    def title(self) -> str:
        return "The vault still uses the retired wikilink dialect"

    @property
    def detail(self) -> str:
        return (
            "The vault still uses `[[wikilinks]]`, which nothing reads any more: "
            "they are not graph edges, not backlinks, and not clickable in the "
            f"file viewer. First example: {self.example}."
        )

    @property
    def remedy(self) -> str:
        return (
            "Preview with `ciao vault-migrate-links` (dry-run by default), then "
            "apply with `ciao vault-migrate-links --apply`. Every rewrite is "
            "recorded, so `ciao vault-unmigrate-links --apply` restores the notes "
            "byte for byte."
        )


#: A walk located a wikilink. The only state that is ever a finding.
LINKS_FOUND = "found"
#: A walk finished and found nothing. Silence, and an honest one.
LINKS_CLEAN = "clean"
#: A walk, or the receipt read in front of it, did not finish. Not silence and
#: not a finding: an unknown, cached so a broken vault is not re-walked and
#: re-logged on every poll, and never stored as ``clean`` — see the module
#: docstring for why that distinction is the whole point.
LINKS_FAILED = "failed"


@dataclass(frozen=True)
class _CacheEntry:
    """One stored verdict, and the situation it was established for.

    Keyed by the vault path alone, with the runtime root and the receipt's
    identity inside ``token``: an answer is looked up by what it is about, and
    reused only for the situation it was computed in. One small entry per vault,
    which is bounded by the number of registered vaults rather than by anything
    that grows with the notes in them.
    """

    token: str
    computed_at: float
    state: str
    example: str = ""


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _Scope:
    """Where the notice would apply, and whether this install can tell.

    ``vault is None`` means the notice cannot apply at all, which is a different
    answer from every one of the three scan states: a caller with no runtime root
    cannot know whether the migration ran, and a **completed** receipt says it
    did. ``read_receipt`` reports only a `status == "migrated"` run, so a receipt
    left by a run that could not write every note does not silence the notice —
    the vault is half-converted and the half is the finding.

    ``readable`` is False when the receipt itself could not be read. That is not
    out of scope and it is not clean: the scope is still known (the runtime root
    and the vault are both there, so the answer is cacheable and worth caching),
    only the evidence is not, and the caller is expected to record that as
    ``LINKS_FAILED``.

    The token is the receipt file's own identity, because the receipt is the
    completion evidence and its identity changes exactly when that evidence does:
    a migration writes and archives one, an un-migration replaces it, and a retry
    of a partial one rewrites it. Every one of those lands a new token, so a
    stored answer is never reused across a change to the thing it is an answer
    about. Two installs in one process — a test, a dev checkout beside a real
    engine — cannot share an answer, because the runtime root and the vault path
    are in the key.
    """

    vault: Path | None = None
    token: str = ""
    readable: bool = True


def _links_scope(config: Any, runtime_dir: Path | None) -> _Scope:
    if runtime_dir is None:
        return _Scope()
    vault_raw = getattr(config, "vault_root", None)
    if vault_raw is None:
        return _Scope()
    vault = Path(vault_raw)
    try:
        from ciao.vault_migrate_links import read_receipt, receipt_path

        completed = read_receipt(runtime_dir)
    except Exception:  # noqa: BLE001 — advisory; an unreadable receipt is unknown
        logger.exception("migration notices: link-migration receipt read failed")
        return _Scope(vault=vault, token=_links_token(runtime_dir, vault), readable=False)
    if completed is not None:
        return _Scope()
    return _Scope(vault=vault, token=_links_token(runtime_dir, vault))


def _links_token(runtime_dir: Path, vault: Path) -> str:
    """The identity of this situation: the install, the vault, the receipt.

    Read through ``receipt_path`` rather than a path spelled out here, so the
    receipt's layout stays the one thing that decides where it lives. A receipt
    that cannot be stat'd is as good as absent, which is the situation its
    absence already describes.
    """
    try:
        from ciao.vault_migrate_links import receipt_path

        stat = receipt_path(runtime_dir).stat()
        stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    except (ImportError, OSError, TypeError):
        stamp = "absent"
    return f"{Path(runtime_dir)}|{vault}|{stamp}"


def _publish(key: str, token: str, state: str, example: str, instant: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(
            token=token, computed_at=instant, state=state, example=example
        )


def _entry_in_window(vault: Path, token: str, instant: float) -> _CacheEntry | None:
    """The stored verdict for exactly this vault, this token and this age.

    One place, so "fresh enough to report" and "stale enough to rescan" cannot
    disagree — which is the same discipline the rest of this module is about. A
    negative age counts as stale: an injected or stepped clock has not reached the
    window it is being measured against.
    """
    with _CACHE_LOCK:
        entry = _CACHE.get(str(vault))
    if entry is None or entry.token != token:
        return None
    if not 0 <= instant - entry.computed_at < LINKS_SCAN_TTL_S:
        return None
    return entry


def resolve_links(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> LinksFinding | None:
    """The finding, established by walking the vault. Never call this on a poll.

    The one walk, for the callers that are allowed to pay for it: the OS audit,
    which is already a full-install pass, and :func:`refresh_links`, which runs
    this on the bounded off-loop executor. It stops at the first note still
    holding a wikilink, so a positive answer is cheap to re-establish and a clean
    one costs the vault — which is the whole reason the Home side reads a stored
    answer instead of calling this.

    Whatever it finds is published, so an audit run warms the strip for free.

    ``now`` is the freshness clock (`time.monotonic` by default) and is
    injectable so a test can place an answer inside or outside the window.
    """
    scope = _links_scope(config, runtime_dir)
    if scope.vault is None:
        return None
    instant = time.monotonic() if now is None else now
    if not scope.readable:
        _publish(str(scope.vault), scope.token, LINKS_FAILED, "", instant)
        return None
    try:
        from ciao.vault_migrate_links import has_unmigrated_links

        example = has_unmigrated_links(scope.vault)
    except Exception:  # noqa: BLE001 — advisory; an unreadable vault is not a notice
        logger.exception("migration notices: wikilink scan failed")
        _publish(str(scope.vault), scope.token, LINKS_FAILED, "", instant)
        return None
    _publish(
        str(scope.vault),
        scope.token,
        LINKS_FOUND if example else LINKS_CLEAN,
        example,
        instant,
    )
    return LinksFinding(vault_root=scope.vault, example=example) if example else None


def cached_links(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> LinksFinding | None:
    """The finding, from the last established answer, without reading a note.

    What Home may call. A finding exists only when a walk actually found a
    wikilink, so a card drawn from this is a claim somebody can act on rather
    than an inference from a receipt's absence — and an install whose last scan
    found nothing gets no card, which is how the detector reaches zero for a
    reason other than "a migration ran". A ``failed`` answer is silence too, and
    for the same reason it is the silence the audit gets: nothing was established.
    """
    scope = _links_scope(config, runtime_dir)
    if scope.vault is None:
        return None
    entry = _entry_in_window(
        scope.vault, scope.token, time.monotonic() if now is None else now
    )
    if entry is None or entry.state != LINKS_FOUND or not entry.example:
        return None
    return LinksFinding(vault_root=scope.vault, example=entry.example)


def links_scan_is_stale(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> bool:
    """Whether Home should start a scan, answered without reading a note.

    False whenever a stored answer of any kind is inside the window — a ``clean``
    one and a ``failed`` one alike — and false whenever the notice cannot apply
    at all (a completed receipt, no runtime root), so a migrated install is not
    woken every 60s to be told nothing again. A ``failed`` answer therefore
    retries once per window rather than once per poll, which is the whole point
    of recording it.
    """
    scope = _links_scope(config, runtime_dir)
    if scope.vault is None:
        return False
    instant = time.monotonic() if now is None else now
    return _entry_in_window(scope.vault, scope.token, instant) is None


async def refresh_links(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> None:
    """Establish the verdict off the event loop, through the bounded executor.

    The work goes through :func:`ciao.async_reads.run_read`, so it is coalesced
    per install and admission-capped with every other vault read, and a cancelled
    caller leaves the worker joinable rather than orphaned. No request awaits
    this; :func:`start_links_scan` is what a route calls.

    The freshness check is repeated inside the worker, because two polls can pass
    the route's gate before either scan is admitted and only one walk is worth
    doing.
    """
    scope = _links_scope(config, runtime_dir)
    if scope.vault is None:
        return
    key = f"migration-notices:links:{scope.vault}"

    def _scan() -> None:
        if not links_scan_is_stale(config, runtime_dir, now=now):
            return
        resolve_links(config, runtime_dir, now=now)

    await run_read(key, _scan)


def start_links_scan(state: Any, config: Any, runtime_dir: Path | None) -> bool:
    """Start the scan detached, at most one at a time. Returns whether it started.

    ``state`` is whatever holds the task handle — Starlette's ``app.state`` in
    the web app. The gate is the stored answer, not the task: a scan inside its
    window is not started at all, and one already in flight is not joined, because
    this poll's answer does not need it and the walk is under way regardless.

    The task's outcome is observed here rather than left to the garbage collector.
    A scan that raises — an executor closed under a restart, say — would otherwise
    surface as "Task exception was never retrieved" at some unrelated moment, and
    a failure nobody looks at is a failure nobody fixes. That is the same reason
    ``async_reads`` retrieves a detached worker's exception.
    """
    if not links_scan_is_stale(config, runtime_dir):
        return False
    running = getattr(state, "links_scan_task", None)
    if running is not None and not running.done():
        return False
    task = asyncio.create_task(refresh_links(config, runtime_dir), name="ciao-links-scan")
    task.add_done_callback(_observe_links_scan)
    state.links_scan_task = task
    return True


def _observe_links_scan(task: "asyncio.Task[None]") -> None:
    """Retrieve a finished scan's outcome, so no failure is left unobserved."""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        logger.warning("Link scan failed: %s", error, exc_info=error)


async def shutdown_links_scan(state: Any) -> None:
    """Cancel a scan still in flight and wait for it to acknowledge.

    Registered on the application's shutdown callbacks, before the vault-read
    executor is closed, so a restart is not left holding a task that is waiting on
    a worker about to be discarded. Cancelling here detaches the awaiter only —
    ``run_read``'s worker runs to completion and stays joinable by a later caller
    — so this is about not leaving a task pending on a closing loop, not about
    stopping the walk.
    """
    task = getattr(state, "links_scan_task", None)
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown must not raise past this point
        logger.exception("Link scan failed while shutting down")


def reset_links_cache() -> None:
    """Drop every stored verdict. Test-only isolation hook."""
    with _CACHE_LOCK:
        _CACHE.clear()


# -- unrehomed person notes --------------------------------------------------


def completed_rehome(runtime_dir: Path) -> dict[str, Any] | None:
    """The receipt of a **completed** person re-homing, or ``None``.

    One reader, deliberately, because this question is asked twice in opposite
    directions — the notice asks "is there none?" and #833's completion check
    asks "is there one?" — and two callers reaching for ``vault-rehome.json`` with
    different ideas of what counts as done is how a notice and a task end up
    disagreeing about the same install.

    The accessor is :func:`ciao.vault_rehome.read_receipt`, which gates on
    ``status`` rather than on the file existing. That is the honest reader for
    both directions:

    * ``partial`` counts as **not** completed. A run that could not write or
      could not move some note left the vault half re-homed, and reading that as
      done is exactly how a half-converted vault reports itself finished.
    * a receipt written before ``status`` existed counts as **completed**. Those
      installs did the work; gating on the field made the notice a permanent
      false positive on precisely the vaults that had already run the migration.
    * a receipt that cannot be parsed, or that is not an object, counts as
      **not** completed. Absence of proof is not proof, in either direction.

    Returns the receipt itself rather than a boolean, because the completion
    check has to name it in its evidence and a bare ``True`` cannot say which
    run it was looking at.
    """
    from ciao.vault_rehome import read_receipt

    return read_receipt(Path(runtime_dir))


@dataclass(frozen=True)
class UnrehomedPeopleFinding:
    """An install whose person notes may still need re-homing.

    ``workspaces`` is the registered workspace count the condition was decided
    on, and it is carried rather than discarded so a caller can say *why* the
    answer is what it is: an install with one workspace has nowhere to misfile a
    note to, so the count is not a detail.

    The wording is the audit's, unchanged. It is deliberately hedged — "may be
    filed in the wrong workspace", "none have been re-homed yet" — because the
    condition is a receipt's absence and nothing here has walked the vault to
    check. A notice that asserted that notes are misfiled would be asserting
    something its own predicate never established, and #833's task prompt has to
    inherit exactly that hedge rather than improve on it.
    """

    workspaces: tuple[str, ...]

    @property
    def title(self) -> str:
        return "Person notes may be filed in the wrong workspace"

    @property
    def detail(self) -> str:
        return (
            "Person notes may be filed in the wrong workspace, and "
            "none have been re-homed yet. A preview lists the "
            "candidates without moving anything."
        )

    @property
    def remedy(self) -> str:
        return (
            "Preview with `ciao vault-rehome` (dry-run by default), "
            "then apply with `ciao vault-rehome --apply`. Every move "
            "and link rewrite is recorded, so `ciao vault-unrehome "
            "--apply` restores the notes and their references."
        )


def rehomed_people_finding(
    config: Any, runtime_dir: Path | None
) -> UnrehomedPeopleFinding | None:
    """The re-home **notice** for this install, or ``None``.

    Two terms, both cheap and read-only, and both of them the audit's own terms
    rather than a stricter version of them:

    * **more than one registered workspace.** With one workspace there is no
      counterpart for a note to be misfiled *from*: ``detect_misfiled_people``
      buckets an untagged note as needing judgement, but its destination comes
      back empty, so there is no move to offer. Reporting it anyway is how a
      fresh install learns to ignore the whole strip.
    * **no completed re-home receipt.** See :func:`completed_rehome` for what
      counts, including why a ``partial`` run keeps the finding and a receipt
      without a ``status`` field does not.

    **This is deliberately the broad answer, and it is not the task's answer.**
    It is a *diagnostic*: "there has never been a re-homing here, and here is the
    command that does one". It cannot know whether anything is misfiled, which is
    why its wording hedges and why nothing here walks a vault — this routine runs
    on every app open. The optional Home card has to say something stronger than
    "a receipt is missing", or it is offered on every fresh conforming install,
    which is the false positive #833's first pass produced and the reason #800
    says a task needs real evidence. That evidence is
    :func:`rehome_legacy_candidates`, and the two answers are allowed to differ:
    the notice is the broad advisory one, the card is the narrow actionable one,
    and a card is a thing an operator is asked to act on.

    ``runtime_dir is None`` cannot apply the notice at all, which is a different
    answer from "there is no receipt": a caller with no runtime root has nowhere
    to read the evidence from, so it cannot say the work was done. That is the
    same distinction the wikilink scope draws with ``vault is None``.

    Raises rather than swallowing: a config that will not name its workspaces,
    or a receipt reader that fails, is an **unknown**, and the two callers answer
    it differently on purpose — ``os_audit`` logs and stays silent rather than
    failing a whole report, while ``update_tasks``' detector turns the raise into
    ``unknown`` so no task is offered on a condition this install could not
    establish. Neither one is allowed to call that ``not applicable``.
    """
    if runtime_dir is None:
        return None
    lister = getattr(config, "workspace_names", None)
    if not callable(lister):
        # A config without a workspace registry exposes none of the accessors
        # this needs, and there is nothing for it to report. Same rule as
        # `vault_location_findings`: a config that cannot answer is not an error
        # on a notice path, it is a surface that has nothing to say.
        return None
    names = tuple(str(name) for name in lister())
    if len(names) < 2:
        return None
    if completed_rehome(runtime_dir) is not None:
        return None
    return UnrehomedPeopleFinding(workspaces=names)


# -- the evidence a re-home TASK needs, as opposed to the notice above --------

# The re-home question's answers, and none of them is another one. Named constants
# because a caller recording one in a state file or a log line has to be able to
# spell it, and because a test asserting "the task is not offered" should say
# *why* rather than only that. A caller with no runtime root is deliberately
# absent from this set: it cannot ask the question at all, and that is raised
# rather than answered.

#: A completed re-homing is recorded. The remedy refuses a second pass, so there
#: is nothing for this task to ask for.
REHOME_ALREADY_COMPLETED = "rehome_already_completed"

#: One registered workspace: nowhere for a note to be misfiled from. The same
#: half the notice keeps.
REHOME_SINGLE_WORKSPACE = "single_workspace"

#: No shared vault directory. After the re-rooting ``config.vault_root`` names a
#: path that does not exist, and ``ciao vault-rehome`` plans
#: ``<vault>/<workspace>/People`` — so on a per-workspace root there is no move
#: it can make and no managed remedy that can make it. Advertising the task there
#: would be offering a command that finds nothing, permanently.
REHOME_NO_SHARED_VAULT = "no_shared_vault"

#: No tag role binds to a registered workspace, so ``detect_misfiled_people``
#: cannot reach a mechanical bucket at all. See :func:`rehome_legacy_candidates`
#: for why that is a proof rather than a guess.
REHOME_NO_ROLE_BINDING = "no_role_binding"

#: The scan ran and found no tag-obvious cross-workspace move. This is the only
#: one of the five that is a statement about the vault rather than about the
#: layout.
REHOME_NO_CANDIDATES = "no_legacy_candidates"

#: The scan ran and found at least one. The only answer that makes a task worth
#: offering.
REHOME_CANDIDATES_FOUND = "legacy_candidates"


@dataclass(frozen=True)
class RehomeCandidates:
    """What the managed re-home command would actually do on this install's vault.

    ``mechanical`` holds the vault-relative paths of the moves the command would
    make, in the order its own plan produced them — the deterministic,
    tag-obvious candidates. Empty is the answer that matters: an install with no
    legacy misfiling has nothing for the task, whatever its receipts say.

    ``conflicts`` and ``needs_judgement`` are counted rather than listed because
    neither of them is offered as work. A conflict is a tag-obvious candidate
    whose destination is taken (two people, same filename, two workspaces) — a
    content decision this engine refuses — and a judgement case is a note with no
    tag naming a workspace, which is a relationship with a person and not a
    misfiling. Both are reported by the run itself, and neither is the legacy
    damage this task exists for.

    ``notes_scanned`` is the scan's own count and nothing more. It is here to make
    the bound visible in a state file, never to imply that a number of notes is a
    number of problems.

    ``reason`` is one of the ``REHOME_*`` constants, and it says which of the
    four cheap gates decided the answer — so a task that is not offered can be
    read as "the plan found nothing" rather than as silence.
    """

    reason: str
    mechanical: tuple[str, ...] = ()
    conflicts: int = 0
    needs_judgement: int = 0
    notes_scanned: int = 0
    scanned: bool = False


def rehome_legacy_candidates(
    config: Any, runtime_dir: Path | None
) -> RehomeCandidates:
    """Whether this install has real legacy misfiled person notes, and the evidence.

    The narrow question, for the optional task, and it is the one #800 asks of a
    catalog row: not "has nobody ever run a migration here" but "is there
    something to migrate". A fresh install that has never needed a migration
    answers no, and answers it without the card ever appearing.

    Four gates before the walk, each of them a proof rather than a guess, so the
    vault is only read on an install where a mechanical candidate is reachable at
    all:

    1. **a completed re-home receipt** (:func:`completed_rehome`). The remedy
       refuses a second pass without ``--force``, so an install that has recorded
       one has nothing for this task to ask — and a card the operator cannot act
       on is the "offered forever" shape #788's upkeep row is about. This also
       keeps the two surfaces consistent at the one point where they must agree:
       a completed receipt silences the notice *and* the card.
    2. **more than one registered workspace**, the notice's own half. One
       workspace has no counterpart for a note to be misfiled from.
    3. **a shared vault that exists.** ``config.vault_root`` is the shared
       layout's directory and, after the re-rooting, a path that is not there
       (``CiaoConfig.vault_scan_targets`` says so in its own docstring). This is
       the same gate ``operator_actions._detect_workspace_unmigrated`` uses. A
       per-workspace root has no workspace segment in its paths, so the command
       has nothing to move there and ``--workspace-name`` changes nothing: this
       install has no managed re-home, and the task must not pretend otherwise.
    4. **at least one tag role bound.** ``resolve_role_workspaces`` maps the tag
       roles (``work``/``personal``) onto registered workspace *names*, and
       ``detect_misfiled_people`` can only reach ``bucket == "mechanical"`` when
       some note's tag names a role that is bound — and the workspace it binds to
       is not the note's own. **One bound role is enough**, and this gate was wrong
       for exactly that reason before #833's R2 review: it demanded two distinct
       bound roles, so an install named ``[work, clientA]`` silently dropped a real
       mechanical candidate (``clientA/People/Mo.md`` tagged ``colleague``, moving
       to ``work/People/Mo.md``) because ``clientA`` plays no role of its own.
       That is a false *negative* on the one install the task exists for, which is
       worse than the false positive it was added to remove.

       The necessary condition is one, not two: a note needs a tag whose role is in
       ``roles`` and whose bound workspace differs from the note's own directory,
       and with no role bound at all ``target_workspaces`` is empty for every note,
       so the command can reach no mechanical candidate whatever the vault holds —
       it drops those notes without even queueing them. So the gate is now exactly
       that: **zero** bound roles skips the walk. This still keeps an install named
       entirely outside the two role vocabularies (``clientA``/``clientB``) from
       paying for a scan every window to hear "no", and it keeps every install
       *with* a bound role honest about the walk it pays for.

       Note what the gate is and is not: it is a necessary condition for the
       *classifier*, checked against the registry alone, so a caller can skip a walk
       it cannot use the answer of. It says nothing about whether any note is
       actually misfiled — that is the plan's question and the only thing that
       answers it.

    Only then is :func:`ciao.vault_rehome.plan_rehome` run, on the shared root,
    over the **registry's** workspace names (``plan_rehome``'s own docstring:
    a caller with a config passes ``config.workspace_names()``; a directory that
    happens to exist is not a workspace). The plan is the source of truth because
    it is the same object the operator's preview prints and the same classifier
    the apply executes, so the task cannot offer work the command would refuse.

    **This walks the vault, and that is the deliberate price of the answer.** Every
    other applicability probe in this module is a receipt read, because a Home
    poll runs on every focus. This one is for an update-task detector, which
    ``update_tasks.evaluate`` calls off the event loop through
    ``ciao.async_reads.run_read`` — coalesced per install, admission-capped with
    every other vault read, and bounded to once per ``APPLICABILITY_TTL_S`` — and
    never from ``operator_actions``. A caller that cannot pay that (the strip,
    the audit) must use :func:`rehomed_people_finding` instead, and the module
    docstring says which is which.

    A ``needs_judgement`` note is deliberately **not** evidence. Untagged person
    notes are ordinary on a fresh install — everyone has contacts whose tags say
    nothing about which workspace they belong to — so treating them as proof of
    legacy misfiling is the false positive again with a different justification.
    Only a tag-obvious cross-workspace move counts, which is the damage the old
    global curation run actually did.

    Raises rather than swallowing, for the same reason the notice does: a config
    with no ``vault_root`` at all, a registry that will not name its workspaces,
    or a receipt read that fails is an **unknown**, and ``update_tasks`` answers
    it ``unknown`` so no card is offered on evidence this install could not read.
    """
    from ciao.vault_rehome import plan_rehome, resolve_role_workspaces

    if runtime_dir is None:
        raise ValueError(
            "the re-home task needs a runtime root to read its receipt from"
        )
    if completed_rehome(runtime_dir) is not None:
        return RehomeCandidates(REHOME_ALREADY_COMPLETED)

    lister = getattr(config, "workspace_names", None)
    if not callable(lister):
        # Same rule as `vault_location_findings`: no registry is a surface with
        # nothing to say, not an install with nothing wrong.
        return RehomeCandidates(REHOME_SINGLE_WORKSPACE)
    names = [str(name) for name in lister() if str(name)]
    if len(names) < 2:
        return RehomeCandidates(REHOME_SINGLE_WORKSPACE)

    vault_raw = getattr(config, "vault_root", None)
    if vault_raw is None:
        raise ValueError("the re-home task needs a vault root to look in")
    vault_root = Path(vault_raw)
    if not vault_root.is_dir():
        # Re-rooted, or never set up: no shared vault, so no managed re-home.
        return RehomeCandidates(REHOME_NO_SHARED_VAULT)

    if not resolve_role_workspaces(names):
        # No tag role binds to a registered workspace, so `detect_misfiled_people`
        # cannot reach a mechanical bucket for any note: `target_workspaces` is
        # empty for every one of them and the command drops them without queueing.
        # One bound role is enough — see gate 4, which was wrong about that until
        # #833's R2 review found a real candidate this test was suppressing.
        return RehomeCandidates(REHOME_NO_ROLE_BINDING)

    plan = plan_rehome(vault_root, workspaces=names)
    if "skipped" in plan:
        # `plan_rehome` skipped the root between the `is_dir()` above and here.
        # It cannot write a move list, so there is nothing to offer; the run
        # itself will say which shape it was.
        return RehomeCandidates(REHOME_NO_SHARED_VAULT, notes_scanned=0)
    mechanical = tuple(
        str(candidate.get("path", "")) for candidate in plan.get("mechanical") or []
    )
    return RehomeCandidates(
        REHOME_CANDIDATES_FOUND if mechanical else REHOME_NO_CANDIDATES,
        mechanical=mechanical,
        conflicts=len(plan.get("conflicts") or []),
        needs_judgement=len(plan.get("needs_judgement") or []),
        notes_scanned=int(plan.get("notes_scanned") or 0),
        scanned=True,
    )
