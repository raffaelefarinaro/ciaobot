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
* :func:`refresh_links` runs :func:`resolve_links` through
  :func:`ciao.async_reads.run_read`, so the walk happens on the bounded vault-read
  executor — coalesced per install, admission-capped, never on the event loop.
  The Home route starts it detached and answers the poll from whatever the last
  scan stored, which is the same trade ``_cached_update_hint`` makes: a card
  appears a poll or two after the first scan rather than on the first render.

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
        """The managed command, the way back, and the two ways it can refuse.

        `vault-relocate` has an apply/undo cycle and updates the registry itself,
        so a remedy that describes moving the folder and hand-editing the
        registry teaches a path the engine refuses and the operator cannot
        reproduce. `--undo` restores the exact previous location and a restart is
        needed either way, so the way back and the restart belong to the same
        sentence as the way forward.

        The refusals belong here rather than in a chat prompt, because both
        surfaces have to say them and only one of them opens a chat: `--apply`
        refuses on a source or destination that is a symlink, on any top-level
        entry it cannot place (`unclassified` — a symlink, most often), on a
        vault containing another registered workspace's root, and on a vault
        outside the install's git worktree, where there is no `git mv` and no
        automatic undo at all. Each of those refusals names what to do next, so
        the remedy points at the refusal rather than restating a route — which
        matters for the last one, because the command itself tells the operator
        to finish that case by hand.
        """
        name = self.workspace
        return (
            f"Preview with `ciao vault-relocate {name}`, then apply with "
            f"`ciao vault-relocate {name} --apply`. It moves this workspace's own "
            "content into the standard folder, updates the registry, and can be "
            f"reversed exactly with `ciao vault-relocate {name} --undo`. "
            "Ciaobot needs a restart (Settings -> Restart) before the new location "
            "takes effect everywhere, including in an open chat. `--apply` refuses "
            "rather than guessing — on a symlink, on any top-level entry it cannot "
            "classify, on a vault holding another workspace's root, and on a vault "
            "outside the install's git worktree, which has no automatic undo here. "
            "Every refusal states what to do next, so read it rather than assuming "
            "the move happened."
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
    example: str


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def _links_scope(config: Any, runtime_dir: Path | None) -> tuple[Path, str] | None:
    """The vault to examine and the identity of this situation, or None.

    None means the notice cannot apply at all, and the two reasons are different
    from a clean answer: a caller with no runtime root cannot know whether the
    migration ran, and a **completed** receipt says it did. `read_receipt` reports
    only a `status == "migrated"` run, so a receipt left by a run that could not
    write every note does not silence the notice — the vault is half-converted and
    the half is the finding.

    The token is the receipt file's own identity, because the receipt is the
    completion evidence and its identity changes exactly when that evidence does:
    a migration writes and archives one, an un-migration replaces it, and a retry
    of a partial one rewrites it. Every one of those lands a new token, so a
    stored answer is never reused across a change to the thing it is an answer
    about. Two installs in one process — a test, a dev checkout beside a real
    engine — cannot share an answer, because the runtime root and the vault path
    are in the key.
    """
    if runtime_dir is None:
        return None
    vault_raw = getattr(config, "vault_root", None)
    if vault_raw is None:
        return None
    try:
        from ciao.vault_migrate_links import read_receipt, receipt_path

        if read_receipt(runtime_dir) is not None:
            return None
        try:
            stat = receipt_path(runtime_dir).stat()
            stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            stamp = "absent"
    except Exception:  # noqa: BLE001 — advisory; a broken receipt is not a notice
        logger.exception("migration notices: link-migration receipt read failed")
        return None
    vault = Path(vault_raw)
    return vault, f"{Path(runtime_dir)}|{vault}|{stamp}"


def _publish(key: str, token: str, example: str, instant: float) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(token=token, computed_at=instant, example=example)


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
    if scope is None:
        return None
    vault, token = scope
    try:
        from ciao.vault_migrate_links import has_unmigrated_links

        example = has_unmigrated_links(vault)
    except Exception:  # noqa: BLE001 — advisory; an unreadable vault is not a notice
        logger.exception("migration notices: wikilink scan failed")
        return None
    _publish(
        str(vault),
        token,
        example,
        time.monotonic() if now is None else now,
    )
    return LinksFinding(vault_root=vault, example=example) if example else None


def cached_links(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> LinksFinding | None:
    """The finding, from the last established answer, without reading a note.

    What Home may call. A finding exists only when a walk actually found a
    wikilink, so a card drawn from this is a claim somebody can act on rather
    than an inference from a receipt's absence — and an install whose last scan
    found nothing gets no card, which is how the detector reaches zero for a
    reason other than "a migration ran".
    """
    scope = _links_scope(config, runtime_dir)
    if scope is None:
        return None
    vault, token = scope
    entry = _entry_in_window(vault, token, time.monotonic() if now is None else now)
    if entry is None or not entry.example:
        return None
    return LinksFinding(vault_root=vault, example=entry.example)


def links_scan_is_stale(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> bool:
    """Whether Home should start a scan, answered without reading a note.

    False whenever the notice cannot apply — a completed receipt, no runtime
    root — so a migrated install is not woken every 60s to be told nothing again.
    """
    scope = _links_scope(config, runtime_dir)
    if scope is None:
        return False
    vault, token = scope
    instant = time.monotonic() if now is None else now
    return _entry_in_window(vault, token, instant) is None


async def refresh_links(
    config: Any, runtime_dir: Path | None, *, now: float | None = None
) -> None:
    """Establish the verdict off the event loop, through the bounded executor.

    Detached from the Home route, so a poll never waits on a vault, and answered
    by the next poll from the stored value — the arrangement
    ``routes_api._cached_update_hint`` already uses for the release lookup. The
    work goes through :func:`ciao.async_reads.run_read`, so it is coalesced per
    install and admission-capped with every other vault read, and a cancelled
    caller leaves the worker joinable rather than orphaned.

    The freshness check is repeated inside the worker, because two polls can pass
    the route's gate before either scan is admitted and only one walk is worth
    doing.
    """
    scope = _links_scope(config, runtime_dir)
    if scope is None:
        return
    vault, _token = scope
    key = f"migration-notices:links:{vault}"

    def _scan() -> None:
        if not links_scan_is_stale(config, runtime_dir, now=now):
            return
        resolve_links(config, runtime_dir, now=now)

    await run_read(key, _scan)


def reset_links_cache() -> None:
    """Drop every stored verdict. Test-only isolation hook."""
    with _CACHE_LOCK:
        _CACHE.clear()
