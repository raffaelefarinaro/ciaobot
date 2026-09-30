"""The migration notices, each probed once and stated once.

Why this module exists
----------------------
Two surfaces answer "what has an upgrade left for this install?": the Home strip
(``operator_actions.detect_actions``, polled every 60s and on window focus) and
the ``upgrade_notices`` section of the OS audit (a diagnostic report, never red).
They used to hold their own copy of two conditions each, and the copies had
drifted in ways only one of them could see:

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
one. No Home-side suppression is an input to any function here, so a card that
#800's catalog step later makes optional and dismissible still cannot silence the
audit.
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
