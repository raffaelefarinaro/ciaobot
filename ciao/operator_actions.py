"""Home-screen housekeeping actions: the operator-facing strip of fixes.

Why this exists
---------------
Several things Ciaobot already detects about its own install — an update, a
vault still speaking the retired vocabulary, person notes filed into the wrong
workspace — are acted on from the Settings audit or a terminal, which is not
where the operator is looking. This module turns those detected conditions
into concrete, clearable actions the PWA renders as tiles on the home screen.
A tile either runs the fix, opens a chat that talks it through, or both, and
disappears only when re-detection says the underlying condition is gone. There
is no ``done`` flag and no first-run suppression: a tile is a view of the
machine, not a record of the operator's intent.

The one hard constraint is cheapness. Detection runs on app open and on every
window focus, so no detector may walk the vault or diff a mirrored tree. The
detectors here read a receipt, compare a registry value, or list a small known
folder. Expensive work happens on the button press, never to decide whether to
draw the button.

Contract, enforced in tests
---------------------------
1. Every detector can reach zero: a condition the user may legitimately decline
   is not an action.
2. Detection is idempotent under re-detection: two passes with nothing changed
   produce byte-identical actions, ids included.
3. Every action offers at least one of ``run_label`` / ``chat_prompt``. An
   action with neither is a notice and belongs in the Settings audit.
4. No detector walks the vault (the ``scan_vault`` spy in the tests proves it).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ciao import proposal_kinds

logger = logging.getLogger(__name__)

# A review queue is "deep" once it holds at least this many pending items. The
# tile is a backlog signal, not a per-item reminder, so a single straggler does
# not hold a permanent tile.
REVIEW_QUEUE_DEPTH = 5


@dataclass(frozen=True)
class OperatorAction:
    """One clearable condition the operator can act on.

    ``id`` is stable and scope-suffixed (``vault-location:work``) so a client
    can accept it by id across renders. ``severity`` is a sort key only, never
    rendered. ``glyph`` is one character shown next to the amber rule.
    """

    id: str
    kind: str
    severity: int
    title: str
    detail: str
    glyph: str
    workspace: str
    run_label: str = ""
    chat_label: str = ""
    chat_prompt: str = ""
    # A view this tile can send the operator to, when a purpose-built surface
    # already exists. The proposal queue has had per-row accept/dismiss, a
    # destination picker, a leak confirm and batch operations since P5, and the
    # tiles offered only "Review in chat" — so the operator was told to discuss
    # 109 items in prose while the buttons for them sat one route away,
    # unreachable from the one place that mentions them.
    view_label: str = ""
    view_route: str = ""
    # Unmissable and not dismissible: a precondition the install cannot get past
    # on its own. Deliberately NOT an app-wide lock — the one realistic cause is
    # an uncommitted vault, and locking the app would take away the assistant the
    # operator needs to fix it. Prominent, permanent, and retryable beats modal.
    blocking: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Serializable form for the API payload."""
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "glyph": self.glyph,
            "workspace": self.workspace,
            "run_label": self.run_label,
            "chat_label": self.chat_label,
            "chat_prompt": self.chat_prompt,
            "view_label": self.view_label,
            "view_route": self.view_route,
            "blocking": self.blocking,
        }


@dataclass
class DetectionContext:
    """Everything a detector may read, none of it expensive.

    ``config`` is a CiaoConfig-like object exposing the workspace registry and
    vault/workspace roots. ``runtime_dir`` holds the migration receipts (falls
    back to the config's ``state_path`` parent). ``schedule_store`` is any
    object exposing ``list_entries()`` (a ScheduleManager or ScheduleStore).
    ``package_status`` is a zero-arg callable returning the package-status dict
    from ``package_version.package_status``, pre-cached; when omitted the
    package detector declines to guess and stays silent.
    """

    config: Any
    runtime_dir: Path | None = None
    schedule_store: Any = None
    package_status: Callable[[], dict[str, Any]] | None = None
    now: datetime | None = None

    @property
    def runtime(self) -> Path | None:
        if self.runtime_dir is not None:
            return self.runtime_dir
        state = getattr(self.config, "state_path", None)
        if state is not None:
            return Path(state).parent
        return None


# -- registry ----------------------------------------------------------------


def detect_actions(context: DetectionContext) -> list[OperatorAction]:
    """Run every registered detector and return the sorted, deduplicated list.

    A detector that raises is logged and skipped so one broken detector never
    sinks the strip. Results are sorted by ``(severity, id)``. No truncation:
    a list that routinely exceeds a handful is the signal this should be a
    page, and silently dropping the tail would hide it.
    """
    actions: list[OperatorAction] = []
    for detect in _DETECTORS:
        try:
            actions.extend(detect(context))
        except Exception:  # noqa: BLE001 — one bad detector must not fail the strip
            logger.exception("operator action detector %s failed", getattr(detect, "__name__", "?"))
    actions.sort(key=lambda action: (action.severity, action.id))
    logger.info("operator actions: %d detected", len(actions))
    return actions


# -- package update ----------------------------------------------------------

_PACKAGE_SEVERITY = 30


def _detect_package_update(context: DetectionContext) -> list[OperatorAction]:
    """Surfaces an installable release when one is available.

    Reads the cached package status; the caller owns the cache so detection
    never blocks on GitHub. With no status provider the condition is unknown,
    which is not a condition to act on, so it stays silent.
    """
    if context.package_status is None:
        return []
    try:
        status = context.package_status()
    except Exception:  # noqa: BLE001 — a broken probe must not fail the strip
        logger.exception("operator actions: package status probe failed")
        return []
    if not status.get("update_available"):
        return []
    latest = status.get("latest_version") or ""
    return [
        OperatorAction(
            id="package-update",
            kind="package-update",
            severity=_PACKAGE_SEVERITY,
            title="A new Ciaobot version is ready to install",
            detail=f"Version {latest} is available and the current install is "
            f"{status.get('current_version') or 'unknown'}.",
            glyph="▲",
            workspace="",
            chat_label="How to install",
            chat_prompt=(
                f"A new Ciaobot version ({latest}) is available. The current "
                "install is updated through Ciaobot.app or the one-line "
                "installer; check Settings for the update, and explain how to "
                "install it on this machine without losing data."
            ),
        )
    ]


# -- vault location ----------------------------------------------------------


def _detect_vault_location(context: DetectionContext) -> list[OperatorAction]:
    """A workspace vault kept outside its standard folder is a chat-only fix.

    Mirrors the ``vault_outside_vault_root`` notice: the standard location is a
    fact of the registry, not a scan, so comparing two resolved paths is cheap.
    Moving an existing vault is a user-owned decision with possible conflicts,
    so this is a chat action, never a mechanical run.
    """
    config = context.config
    resolver = getattr(config, "workspace_vault_root", None)
    standardizer = getattr(config, "canonical_workspace_vault_root", None)
    lister = getattr(config, "workspace_names", None)
    if not callable(resolver) or not callable(standardizer) or not callable(lister):
        return []
    actions: list[OperatorAction] = []
    for name in lister():
        try:
            actual = Path(resolver(name)).resolve()
            standard = Path(standardizer(name)).resolve()
        except Exception:  # noqa: BLE001 — advisory; a bad registry must not fail
            continue
        if actual == standard or not actual.is_dir():
            continue
        actions.append(
            OperatorAction(
                id=f"vault-location:{name}",
                kind="vault-location",
                severity=20,
                title=f"The {name} vault is not in its standard folder",
                detail=(
                    f"Workspace '{name}' keeps its vault at {actual}; its standard "
                    f"location is {standard}."
                ),
                glyph="⌂",
                workspace=name,
                chat_label="Fix in chat",
                chat_prompt=(
                    f"The vault for workspace '{name}' lives at {actual}, but its "
                    f"standard location is {standard}. Inspect both locations, ask "
                    "before resolving any conflicts, identify which files are vault "
                    "content, make a backup before moving anything, then move the "
                    "approved content and update the active workspace registry to "
                    f"the standard path {standard}. Verify the workspace before "
                    "removing the backup."
                ),
            )
        )
    return actions


# -- unrehomed people --------------------------------------------------------


def _count(value: Any) -> int:
    """How many, from a receipt field that may be a count or a list.

    The tile read `mechanical`/`conflicts`, which this receipt has never had, and
    `needs_judgement`, which is a LIST. So it reported "0 to move" while 87 notes
    were recorded as moved, and interpolated a list of dicts straight into the
    prose the user reads. Counting defensively is the point: a detail string must
    never render a container, whatever the schema does next.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return 0


def _detect_workspace_unmigrated(context: DetectionContext) -> list[OperatorAction]:
    """The re-rooting has not run, and this install needs it.

    The migration is attempted automatically at every start, so reaching this
    means it REFUSED — and the only realistic reason is uncommitted vault
    changes, which is the gate working: `git checkout` has to stay a working undo
    before a release rewrites someone's layout.

    Silent on an install with nothing to migrate. A single workspace, or no
    registered workspace at all, has no second root to separate from and the
    shared layout is already correct for it.
    """
    config = context.config
    runtime = context.runtime
    lister = getattr(config, "workspace_names", None)
    if not callable(lister):
        return []
    names = [n for n in lister() if n]
    if len(names) <= 1:
        return []
    vault_root = getattr(config, "vault_root", None)
    if vault_root is None or not Path(vault_root).is_dir():
        # No shared vault to move: either already migrated or never set up.
        return []
    try:
        from ciao.workspace_reroot import peek_receipt, read_receipt

        if read_receipt(runtime) is not None:
            return []
        receipt = peek_receipt(runtime)
    except Exception:  # noqa: BLE001 — advisory
        logger.exception("operator actions: re-root check failed")
        return []

    refusals = list((receipt or {}).get("refusals") or [])
    if refusals:
        # Every refusal, not just the first. A run can be blocked by several
        # things at once — two uncommitted files, or an unregistered directory
        # AND a non-empty destination — and fixing the one shown leaves the tile
        # exactly where it was, with no hint that anything else was wrong. Capped
        # at three so the tile stays a tile; the count says how many are hidden.
        shown = refusals[:3]
        rest = len(refusals) - len(shown)
        joined = "; ".join(shown) + (f"; and {rest} more" if rest > 0 else "")
        detail = (
            "Each workspace needs its own agent root, and the move refused: "
            + joined
        )
    else:
        detail = (
            "Each workspace needs its own agent root so its notes, guide and "
            "skills stop being shared. The move has not run yet."
        )
    return [
        OperatorAction(
            id="workspace-unmigrated",
            kind="workspace-unmigrated",
            severity=0,
            title="Workspaces are still sharing one vault",
            detail=detail,
            glyph="⇄",
            workspace="",
            blocking=True,
            run_label="Separate them now",
            chat_label="Ask what is blocking it",
            chat_prompt=(
                "This install still keeps every workspace in one shared vault, "
                "and the automatic separation refused. Run "
                "`ciao workspace-reroot --workspace .` to print the plan and every "
                "refusal, then clear the causes. You may: commit uncommitted vault "
                "changes (git checkout has to stay a working undo, which is the "
                "whole reason it refused); move or empty a destination directory "
                "that already exists and is not empty; and for a vault directory "
                "the plan calls unclassified, either register it as a workspace or "
                "move it out of the vault — ask me which, because that one is a "
                "decision about my notes, not a cleanup. Re-run the command after "
                "each fix so the refusal list shrinks in front of you.\n\n"
                "Do NOT pass --apply. When the command prints no refusals, say so "
                "and tell me the button is ready; the migration itself is mine to "
                "press."
            ),
        )
    ]


_QUEUE_RELATIVE = ("Workspace", "Memory-Proposals.md")


def _queued_rehome_rows(context: DetectionContext) -> int | None:
    """How many re-home rows the review queue currently holds, or None.

    This is the number the tile's own button opens, which is the only number a
    tile should quote. The receipt cannot supply it: it is written once, at
    migration time, so it cannot know that the operator has dismissed rows since,
    or that a later rule resolved some of them.

    Counting bullets rather than re-detecting: `detect_misfiled_people` walks
    every person note, which is not something to do on every strip render, and
    the queue is two small files.
    """
    config = context.config
    resolver = getattr(config, "workspace_vault_root", None)
    lister = getattr(config, "workspace_names", None)
    if not callable(resolver) or not callable(lister):
        return None
    total = 0
    seen = False
    for name in lister():
        try:
            queue = Path(resolver(name)).joinpath(*_QUEUE_RELATIVE)
        except Exception:  # noqa: BLE001 — advisory
            continue
        if not queue.is_file():
            continue
        try:
            text = queue.read_text(encoding="utf-8")
        except OSError:
            continue
        seen = True
        total += sum(
            1 for line in text.splitlines()
            if line.lstrip().startswith(("- ", "* ")) and "[rehome]" in line
        )
    return total if seen else None


def _detect_unrehomed_people(context: DetectionContext) -> list[OperatorAction]:
    """Person notes that a global curation run may have filed wrong.

    Detected from the re-home **receipt** alone, never a walk of the person
    notes. Same gate as the Settings audit: with one registered workspace every
    candidate has an empty destination, so there is nothing to move and firing
    would offer an unactionable tile. Absent receipt, or a receipt that is not
    ``migrated``, means the damage was never fixed. The fix is a judged
    migration, so this is chat-only.
    """
    runtime = context.runtime
    config = context.config
    if runtime is None:
        return []
    lister = getattr(config, "workspace_names", None)
    if not callable(lister):
        return []
    names = list(lister())
    if len(names) <= 1:
        return []
    try:
        from ciao.vault_rehome import peek_receipt

        receipt = peek_receipt(runtime)
    except Exception:  # noqa: BLE001 — advisory
        logger.exception("operator actions: re-home check failed")
        return []
    if receipt is not None:
        moved = _count(receipt.get("moves"))
        # The QUEUE decides this number, not the receipt. Two bugs lived in the
        # old line: it summed `needs_judgement` and `proposals`, which are not
        # disjoint — `proposals` records the queue entries written FOR those
        # judgement cases — and both are frozen at migration time, so neither
        # knows the operator has dismissed rows since or that a later rule
        # resolved some. On the reference install that produced four different
        # numbers for one thing: 16 on the tile, 15 in the receipt, 14 rows in
        # the queue the tile's own button opens, and 12 live candidates.
        queued = _queued_rehome_rows(context)
        undecided = queued if queued is not None else _count(receipt.get("needs_judgement"))
        # An ABSENT status counts as applied. `vault_rehome` only started writing
        # the field when its survey mode was added, so a receipt written before
        # that records a COMPLETED re-home — and reading those as unfinished made
        # this tile a permanent false positive on exactly the installs that had
        # done the work. The reference install shows it: 87 moves and 165 link
        # rewrites recorded, no status, tile still firing a day later.
        applied = receipt.get("status", "migrated") == "migrated"
        # Applied with nothing left to decide is genuinely finished. Applied with
        # notes still needing a decision is NOT: the mechanical moves are done and
        # a human still owes an answer on the rest, which a status-only check
        # hides the moment the field starts being written.
        if applied and undecided == 0:
            return []
        if applied:
            detail = (
                f"{moved} person note(s) were re-homed. {undecided} still need a "
                "decision because no tag names a workspace, and are queued as "
                "proposals for review."
            )
        else:
            detail = (
                "Person notes may be filed in the wrong workspace: a survey found "
                f"{moved} to move and {undecided} needing a decision, and none of "
                "it has been applied yet."
            )
    else:
        detail = (
            "Person notes may be filed in the wrong workspace, but no re-home "
            "survey has been recorded yet."
        )
    return [
        OperatorAction(
            id="vault-unrehomed-people",
            kind="unrehomed-people",
            severity=20,
            title="Person notes may be in the wrong workspace",
            detail=detail,
            glyph="¶",
            workspace="",
            view_label="Open queue",
            view_route="/proposals",
            chat_label="Review in chat",
            chat_prompt=(
                "Some person notes may be filed in the wrong workspace's vault. "
                "Preview the candidates with `ciao vault-rehome` (dry-run by "
                "default), then apply the tag-obvious moves with "
                "`ciao vault-rehome --apply`. Notes with no workspace-naming tag "
                "need a decision and are queued, never moved automatically. Every "
                "move and link rewrite is recorded, so `ciao vault-unrehome "
                "--apply` can restore the notes and their references."
            ),
        )
    ]


# -- vault vocabulary --------------------------------------------------------


def _detect_vault_vocabulary(context: DetectionContext) -> list[OperatorAction]:
    """Frontmatter types that the canonical-vocabulary migration could not resolve.

    Read from the ``vault-vocabulary.json`` receipt only. The migration is run
    at install (see ``sync_skills``), which writes the receipt with the
    ``unresolved`` types it declined to guess. Those are real categorisation
    decisions the operator must make; the tile surfaces them. The run button
    re-applies the aliased renames (a note written since the migration may have
    reintroduced an alias) and rewrites the receipt, so a clean re-run clears
    the tile.
    """
    runtime = context.runtime
    if runtime is None:
        return []
    try:
        from ciao.vault_migration import read_receipt

        receipt = read_receipt(runtime)
    except Exception:  # noqa: BLE001 — a broken receipt must not fail the strip
        logger.exception("operator actions: vocabulary receipt read failed")
        return []
    if receipt is None:
        return []
    unresolved = receipt.get("unresolved") or {}
    if not unresolved:
        return []
    kinds = ", ".join(sorted(unresolved))
    return [
        OperatorAction(
            id="vault-vocabulary",
            kind="vault-vocabulary",
            severity=20,
            title="Some notes use a retired type vocabulary",
            detail=(
                f"{len(unresolved)} frontmatter type(s) have no canonical "
                f"equivalent: {kinds}. They need a categorisation decision."
            ),
            glyph="φ",
            workspace="",
            run_label="Apply mechanical renames",
            chat_label="Decide in chat",
            chat_prompt=(
                "Some vault notes use a retired frontmatter type with no "
                "canonical equivalent. The types are "
                f"{kinds}. For each one, choose the canonical type it maps to "
                "and update the frontmatter `type:` line. Report any notes you "
                "are unsure about rather than guessing."
            ),
        )
    ]


# -- unmigrated links --------------------------------------------------------


def _detect_unmigrated_links(context: DetectionContext) -> list[OperatorAction]:
    """A vault still written in the retired wikilink dialect.

    Cheap path: an ``existing``-mode vault is the only one that can carry
    wikilinks (a ``scratch`` install is created conformant, so it is skipped
    entirely and reaches zero), and an absent link-migration receipt is the
    signal it was never converted. The exact wikilink walk belongs to the
    button press, never to deciding to draw the tile.
    """
    config = context.config
    runtime = context.runtime
    if runtime is None:
        return []
    mode = getattr(config, "vault_mode", "scratch") or "scratch"
    if mode != "existing":
        return []
    try:
        from ciao.vault_migrate_links import read_receipt

        receipt = read_receipt(runtime)
    except Exception:  # noqa: BLE001
        logger.exception("operator actions: link migration receipt read failed")
        return []
    if receipt is not None:
        return []
    return [
        OperatorAction(
            id="vault-unmigrated-links",
            kind="unmigrated-links",
            severity=20,
            # Worded conditionally on purpose. This detector runs on every app
            # open and window focus, so it cannot call has_unmigrated_links,
            # which walks the vault. It knows only that the vault was adopted
            # and no migration receipt exists, which does NOT establish that a
            # wikilink is present: an adopted vault written in markdown links
            # from the start satisfies both and contains nothing to convert.
            # The audit's own notice does run the accurate check and may
            # legitimately stay silent where this tile speaks.
            title="The vault may still use the retired wikilink dialect",
            detail=(
                "This vault was adopted and no link migration has been recorded, "
                "so it may still contain `[[wikilinks]]`, which nothing reads as "
                "graph edges, backlinks, or clickable links. The preview below "
                "reports exactly what would change, and finds nothing if the "
                "vault is already clean."
            ),
            glyph="🔗",
            workspace="",
            chat_label="Convert in chat",
            chat_prompt=(
                "The vault may still contain retired `[[wikilinks]]`. Preview the "
                "conversion with `ciao vault-migrate-links` (dry-run by default), "
                "then apply it with `ciao vault-migrate-links --apply`. Every "
                "rewrite is recorded, so `ciao vault-unmigrate-links --apply` "
                "restores the notes byte for byte."
            ),
        )
    ]


# -- missed one-time schedules ----------------------------------------------


def _detect_missed_schedules(context: DetectionContext) -> list[OperatorAction]:
    """One collapsed tile for every missed one-time schedule.

    A ``once`` schedule whose target time has passed and which never fired is a
    stale reminder that would otherwise sit in the schedule list forever, or
    auto-fire un-backdated on the next server start. The detector counts them
    by reading the schedule store, never by dispatching. One tile for the whole
    pile, so a burst of missed one-timers does not crowd the strip.
    """
    store = context.schedule_store
    if store is None:
        return []
    try:
        entries = store.list_entries()
    except Exception:  # noqa: BLE001 — a broken store must not fail the strip
        logger.exception("operator actions: schedule store read failed")
        return []
    now = context.now or datetime.now(UTC)
    missed: list[str] = []
    for entry in entries:
        if entry.frequency != "once" or not entry.enabled:
            continue
        if entry.last_triggered_on:
            continue
        if not entry.run_at_date:
            continue
        try:
            tz = ZoneInfo(entry.timezone_name or "UTC")
            target = datetime.fromisoformat(entry.run_at_date).replace(tzinfo=tz)
        except (ValueError, AttributeError):
            continue
        if now >= target:
            missed.append(entry.schedule_id)
    if not missed:
        return []
    return [
        OperatorAction(
            id="missed-schedules",
            kind="missed-schedules",
            severity=10,
            title=f"{len(missed)} one-time reminder(s) were missed",
            detail=(
                f"The server was down past {len(missed)} one-time reminder(s). "
                "They were not auto-fired because they are stale by now."
            ),
            glyph="⏰",
            workspace="",
            run_label="Fire now",
            chat_label="Review in chat",
            chat_prompt=(
                f"{len(missed)} one-time reminder(s) were missed while the server "
                "was off and were not auto-fired because they are stale. Fire the "
                "ones that are still relevant, or dismiss the rest."
            ),
        )
    ]


# -- review queue depth ------------------------------------------------------


def _review_queue_depth(context: DetectionContext) -> int:
    """Pending memory-proposal bullets plus skill-proposal files, across workspaces.

    Both queues are counted through the one shared bullet pattern and a
    directory listing — never through the ``/api/proposals`` route, which walks
    every person note to build its rehome signal. Cheap by construction: a few
    known queue files and one small folder per workspace.
    """
    config = context.config
    lister = getattr(config, "workspace_names", None)
    resolver = getattr(config, "workspace_vault_root", None)
    if not callable(lister) or not callable(resolver):
        return 0
    total = 0
    for name in lister():
        try:
            root = resolver(name)
        except Exception:  # noqa: BLE001 — a missing workspace must not fail
            continue
        queue = Path(root) / "Workspace" / "Memory-Proposals.md"
        try:
            if queue.is_file():
                for line in queue.read_text(encoding="utf-8").splitlines():
                    if proposal_kinds.BULLET_RE.match(line):
                        total += 1
        except (OSError, UnicodeDecodeError):
            pass
        skill_dir = Path(root) / "Workspace" / "Skill-Proposals"
        try:
            if skill_dir.is_dir():
                # Counted by iterating: Path.glob returns a generator, so len()
                # raises TypeError, which detect_actions swallows as a broken
                # detector. That made the whole review-queue tile disappear
                # exactly when a Skill-Proposals folder existed, which is the
                # only case it matters in.
                total += sum(1 for _ in skill_dir.glob("*.md"))
        except OSError:
            pass
    return total


def _detect_review_queue(context: DetectionContext) -> list[OperatorAction]:
    """A deep review queue is a pile of operator decisions, surfaced as one tile.

    Under the depth threshold it is a normal state of a working install, not an
    action — which is what keeps the tile at zero for a healthy vault.
    """
    depth = _review_queue_depth(context)
    if depth < REVIEW_QUEUE_DEPTH:
        return []
    return [
        OperatorAction(
            id="review-queue-depth",
            kind="review-queue-depth",
            severity=5,
            title=f"{depth} proposals are waiting for a review",
            detail=(
                "Memory-proposal bullets and skill-proposal files have piled up "
                "past the threshold. Review them to keep the queues current."
            ),
            glyph="◌",
            workspace="",
            # The queue is the point of this tile, and it already has per-row
            # accept/dismiss, a destination picker, a leak confirm and batch
            # operations. Offering only chat asked the operator to work through
            # 109 items in prose.
            view_label="Open queue",
            view_route="/proposals",
            chat_label="Review in chat",
            chat_prompt=(
                f"There are {depth} pending memory and skill proposals across "
                "the workspaces. List them with `ciao vault-search 'proposal'` "
                "or by reviewing each workspace's `Workspace/Memory-Proposals.md` "
                "and `Workspace/Skill-Proposals/`, then promote or dismiss each one."
            ),
        )
    ]


# -- post-migration drift (§11.2) --------------------------------------------
#
# Four read-only detectors over the per-root layout, each mirroring a drift
# `workspace_reroot.repair` already knows how to fix, so the tile gets a run
# button instead of prose. Read-only on purpose: a detector runs on every strip
# render, and one that repaired as a side effect of being looked at would make
# "what is wrong" unanswerable.
#
# All four are silent before the re-rooting. Before it there is one agent root,
# one guide and one catalog, so "this root has no assets" is not drift — it is
# the layout.

_DRIFT_SEVERITY = 15


def _rerooted_targets(context: DetectionContext) -> list[tuple[str, Path]]:
    """(workspace, agent root) per registered workspace, or [] before re-rooting."""
    config = context.config
    getter = getattr(config, "agent_root_targets", None)
    install = getattr(config, "workspace_root", None)
    if not callable(getter) or install is None:
        return []
    try:
        targets = [(str(name), Path(root)) for root, name in getter()]
    except Exception:  # noqa: BLE001 — advisory
        return []
    # One target whose root IS the install root means the shared layout.
    if len(targets) <= 1 and any(root == Path(install) for _n, root in targets):
        return []
    return [(name, root) for name, root in targets if name]


def _detect_workspace_root_missing(context: DetectionContext) -> list[OperatorAction]:
    """A registered workspace with no directory on disk.

    `repair` recreates the root and installs its assets, so this is a run button.
    Reported per workspace rather than as one tile: the fix is per root, and a
    count tells the operator nothing about which of their workspaces is gone.
    """
    actions: list[OperatorAction] = []
    for name, root in _rerooted_targets(context):
        if root.is_dir():
            continue
        actions.append(
            OperatorAction(
                id=f"workspace-root-missing:{name}",
                kind="workspace-root-missing",
                severity=_DRIFT_SEVERITY,
                title=f"The {name} workspace has no folder",
                detail=(
                    f"'{name}' is registered but {root} does not exist, so its "
                    "guide, skills and notes have nowhere to live."
                ),
                glyph="⌂",
                workspace=name,
                run_label="Recreate it",
            )
        )
    return actions


def _detect_workspace_assets_stale(context: DetectionContext) -> list[OperatorAction]:
    """A root whose generated agent assets no longer match its catalog.

    The catalog (`skills/`, `commands/`, `subagents/`) is the source; `.claude/`,
    `.agents/` and friends are generated from it. A root holding a catalog but no
    generated directory means the provider sees none of them.
    """
    actions: list[OperatorAction] = []
    for name, root in _rerooted_targets(context):
        if not root.is_dir():
            continue   # the missing-root detector owns that case
        missing: list[str] = []
        if (root / "skills").is_dir() and not (root / ".claude" / "skills").exists():
            missing.append(".claude/skills")
        if (root / "commands").is_dir() and not (root / ".claude" / "commands").exists():
            missing.append(".claude/commands")
        if (root / "subagents").is_dir() and not (root / ".claude" / "agents").exists():
            missing.append(".claude/agents")
        if not (root / "CLAUDE.md").is_file():
            missing.append("CLAUDE.md")
        if not missing:
            continue
        actions.append(
            OperatorAction(
                id=f"workspace-assets-stale:{name}",
                kind="workspace-assets-stale",
                severity=_DRIFT_SEVERITY,
                title=f"The {name} workspace is missing generated assets",
                detail=(
                    f"'{name}' has a catalog but no {', '.join(missing)}, so its "
                    "agents cannot see its skills, commands or guide."
                ),
                glyph="⌗",
                workspace=name,
                run_label="Rebuild them",
            )
        )
    return actions


def _detect_skill_triage_pending(context: DetectionContext) -> list[OperatorAction]:
    """Skills the migration could not attribute to one workspace.

    The migration writes a triage file rather than guessing which root owns a
    customised skill, because guessing hands one workspace's tooling to another.
    Chat-only: every entry is a judgement about what the skill is for.
    """
    runtime = context.runtime
    if runtime is None:
        return []
    triage = Path(runtime) / "migration" / "skills-triage.md"
    if not triage.is_file():
        return []
    try:
        lines = [
            line for line in triage.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith(("- ", "* "))
        ]
    except OSError:
        return []
    if not lines:
        return []
    return [
        OperatorAction(
            id="skill-triage-pending",
            kind="skill-triage-pending",
            severity=_DRIFT_SEVERITY,
            title=f"{len(lines)} skill(s) need a workspace",
            detail=(
                "The separation could not tell which workspace these skills "
                "belong to, so it left them for a decision rather than handing "
                "one workspace's tooling to another."
            ),
            glyph="✦",
            workspace="",
            chat_label="Decide with me",
            chat_prompt=(
                f"Read `{triage}` and walk me through each skill it lists. For "
                "each one, say what it does and which workspace it looks like it "
                "belongs to, then ask me to confirm before moving anything. Move "
                "an approved skill into that workspace's `skills/` directory and "
                "run `ciao sync-skills` for that root. Leave anything I do not "
                "confirm exactly where it is."
            ),
        )
    ]


def _detect_mcp_uncomposed(context: DetectionContext) -> list[OperatorAction]:
    """A shared ``.mcp.json`` that no root inherited, so no chat can reach it.

    A chat runs with its agent root as cwd, and a project-scoped ``.mcp.json`` is
    read from that cwd. After the re-rooting the only copy sits at the install
    root, which is nobody's cwd — so every server configured there became
    unreachable, silently, with the file still sitting right there.

    Chat-only, and deliberately not a copy button. ``.mcp.json`` grants
    credentialed access: duplicating it into every root would hand a work chat a
    personal server and the reverse. Which root may reach which server is the
    operator's decision, which is why the migration reported this rather than
    composing it.
    """
    config = context.config
    install = getattr(config, "workspace_root", None)
    if install is None:
        return []
    shared = Path(install) / ".mcp.json"
    if not shared.is_file():
        return []
    targets = _rerooted_targets(context)
    if not targets:
        return []
    without = [name for name, root in targets if not (root / ".mcp.json").is_file()]
    if not without:
        return []
    try:
        import json

        data = json.loads(shared.read_text(encoding="utf-8"))
        servers = sorted(data.get("mcpServers") or data.get("servers") or {})
    except (OSError, ValueError):
        servers = []
    named = ", ".join(servers) if servers else "the configured servers"
    return [
        OperatorAction(
            id="workspace-mcp-uncomposed",
            kind="workspace-mcp-uncomposed",
            severity=_DRIFT_SEVERITY,
            title=f"{len(servers) or ''} MCP server(s) are unreachable from chats".strip(),
            detail=(
                f"{named} are configured in {shared}, but a chat runs from its own "
                f"workspace folder and {', '.join(without)} has no .mcp.json — so "
                "nothing can reach them."
            ),
            glyph="⊘",
            workspace="",
            chat_label="Split them with me",
            chat_prompt=(
                f"My MCP servers ({named}) are configured in `{shared}`, which no "
                "chat reads any more: each workspace now runs from its own folder. "
                "For each server, tell me what it connects to and ask me which "
                "workspaces should be allowed to reach it — do not assume all of "
                "them, because these grant credentialed access and a work chat "
                "reaching a personal server (or the reverse) is the thing to avoid. "
                "Then write a `.mcp.json` into each approved workspace folder "
                "containing only that workspace's servers, and tell me when to "
                "delete the shared one. Never copy the whole file into every root."
            ),
        )
    ]


_IGNORED_ENV_VARS: tuple[tuple[str, str], ...] = (
    ("CLAUDE_DEFAULT_MODEL_PERSONAL", "set a workspace's default_model in workspaces.json"),
    ("CLAUDE_DEFAULT_MODEL_WORK", "set a workspace's default_model in workspaces.json"),
    ("CIAO_DISALLOWED_TOOLS_PERSONAL", "set a workspace's disallowed_tools in workspaces.json"),
    ("CIAO_DISALLOWED_TOOLS_WORK", "set a workspace's disallowed_tools in workspaces.json"),
)


def _detect_legacy_env_ignored(context: DetectionContext) -> list[OperatorAction]:
    """Environment variables the engine no longer reads.

    These described the two hardcoded `personal`/`work` names and went with the
    bootstrap registry that manufactured them. A variable that is set and silently
    ignored is worse than one that never existed: the operator believes a setting
    is in effect. Chat-only — the fix edits `.env`, which is theirs.
    """
    source = getattr(context.config, "env_source", None) or os.environ
    stale = [(name, hint) for name, hint in _IGNORED_ENV_VARS if str(source.get(name, "")).strip()]
    if not stale:
        return []
    names = ", ".join(name for name, _hint in stale)
    return [
        OperatorAction(
            id="legacy-env-ignored",
            kind="legacy-env-ignored",
            severity=_DRIFT_SEVERITY,
            title=f"{len(stale)} setting(s) in .env are no longer read",
            detail=(
                f"{names} described the old hardcoded personal/work pair and are "
                "ignored now, so whatever they say is not in effect."
            ),
            glyph="⚑",
            workspace="",
            chat_label="Move them for me",
            chat_prompt=(
                f"These variables in my `.env` are no longer read by the engine: "
                f"{names}. For each one, tell me its current value and the "
                "workspace it was meant for, then move the setting onto that "
                "workspace in `.runtime/workspaces.json` (`default_model` and "
                "`disallowed_tools` are per-workspace fields there). Ask before "
                "changing a value rather than assuming the old one still reflects "
                "what I want, and comment the variable out of `.env` once its "
                "setting has a new home."
            ),
        )
    ]


_DETECTORS: list[Callable[[DetectionContext], list[OperatorAction]]] = [
    _detect_workspace_unmigrated,
    _detect_package_update,
    _detect_vault_location,
    _detect_unrehomed_people,
    _detect_vault_vocabulary,
    _detect_unmigrated_links,
    _detect_missed_schedules,
    _detect_review_queue,
    _detect_workspace_root_missing,
    _detect_workspace_assets_stale,
    _detect_skill_triage_pending,
    _detect_legacy_env_ignored,
    _detect_mcp_uncomposed,
]


# -- run dispatch -----------------------------------------------------------


def run_action(action_id: str, context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Perform the mechanical work for one action id.

    Returns ``(result, summary_text)``. Raises :class:`ValueError` for an
    unknown action id (the caller maps that to 404) and lets any operational
    error propagate so the route can report the failure. ``summary_text`` is a
    short human line the client can show while the strip re-renders.
    """
    if action_id == "package-update":
        return _run_package_update(context)
    if action_id == "vault-vocabulary":
        return _run_vault_vocabulary(context)
    if action_id == "missed-schedules":
        return _run_missed_schedules(context)
    if action_id == "workspace-unmigrated":
        return _run_workspace_reroot(context)
    if action_id.startswith(("workspace-root-missing:", "workspace-assets-stale:")):
        return _run_workspace_repair(context)
    raise ValueError(f"unknown operator action id: {action_id}")


def _run_workspace_repair(context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Reconcile every root to the registry — the `--repair` pass, as a button.

    Not scoped to the one workspace the tile named. `repair` is idempotent and
    whole-install by design, and a root that drifted is rarely the only one: the
    same interrupted sync or hand-edit usually touched its siblings. Repairing
    everything is also what makes a second press a no-op rather than a partial fix.
    """
    from ciao.workspace_reroot import repair

    config = context.config
    runtime = context.runtime
    if runtime is None:
        raise RuntimeError("no runtime directory, so there is no receipt to repair against")
    result = repair(
        Path(config.workspace_root),
        Path(runtime),
        list(config.workspace_names()),
    )
    status = str(result.get("status", ""))
    if status == "not_rerooted":
        raise RuntimeError(str(result.get("reason") or "this install has not re-rooted yet"))
    errors = result.get("errors") or []
    if errors:
        raise RuntimeError(str(errors[0].get("error", errors[0])))
    repaired = result.get("repaired") or []
    reported = result.get("reported") or []
    if not repaired:
        return result, "Nothing needed repairing."
    drifts = ", ".join(sorted({str(item.get("drift", "")) for item in repaired}))
    tail = f"; {len(reported)} left for a decision" if reported else ""
    return result, f"Repaired {len(repaired)} item(s): {drifts}{tail}."


def _run_workspace_reroot(context: DetectionContext) -> tuple[dict[str, Any], str]:
    """Retry the re-rooting the automatic attempt refused.

    Same entry point the upgrade uses, so the button cannot drift from what
    startup does. A refusal raises, which the route renders as a failed tile
    carrying the reason rather than silently re-offering the button.
    """
    from ciao.workspace_reroot import migrate_if_needed

    result = migrate_if_needed(context.config)
    status = str(result.get("status", ""))
    if status == "migrated":
        moves = len(result.get("applied") or [])
        flagged = len((result.get("stranded_sessions") or {}).get("flagged") or [])
        return result, (
            f"Separated {len(result.get('workspaces') or [])} workspace(s): "
            f"{moves} moves, {flagged} chat(s) will start a fresh session."
        )
    if status in {"already_migrated", "not_applicable"}:
        return result, "Nothing to separate."
    reasons = result.get("refusals") or [result.get("reason") or "no reason recorded"]
    raise RuntimeError(str(reasons[0]))


def _run_package_update(context: DetectionContext) -> tuple[dict[str, Any], str]:
    from ciao.package_version import update_package

    result = update_package()
    if result.get("ok"):
        return result, "Update started."
    reason = result.get("error") or "This checkout cannot self-update."
    raise RuntimeError(reason)


def _run_vault_vocabulary(context: DetectionContext) -> tuple[dict[str, Any], str]:
    config = context.config
    vault_root = getattr(config, "vault_root", None)
    runtime = context.runtime
    if vault_root is None or runtime is None:
        raise RuntimeError("vault or runtime is not configured")
    from ciao.vault_migration import migrate_vault_vocabulary, write_receipt

    summary = migrate_vault_vocabulary(Path(vault_root), apply=True)
    if "skipped" in summary:
        raise RuntimeError(str(summary["skipped"]))
    # Rewrite the receipt so the next detection reflects the re-scan: resolved
    # types clear the tile, anything still unresolved keeps it.
    write_receipt(runtime, {"renamed": summary["renamed"], "unresolved": summary["unresolved"]})
    renamed = len(summary.get("renamed", []))
    failed = len(summary.get("failed", []))
    return summary, f"{renamed} type(s) renamed, {failed} failed."


def _run_missed_schedules(context: DetectionContext) -> tuple[dict[str, Any], str]:
    manager = context.schedule_store
    if manager is None or not hasattr(manager, "dispatch_now"):
        raise ValueError("no schedule manager to fire missed one-timers")
    now = context.now or datetime.now(UTC)
    fired: list[str] = []
    errors: list[str] = []
    for entry in manager.list_entries():
        if entry.frequency != "once" or not entry.enabled:
            continue
        if entry.last_triggered_on:
            continue
        if not entry.run_at_date:
            continue
        try:
            tz = ZoneInfo(entry.timezone_name or "UTC")
            target = datetime.fromisoformat(entry.run_at_date).replace(tzinfo=tz)
        except (ValueError, AttributeError):
            continue
        if now < target:
            continue
        try:
            manager.dispatch_now(entry.schedule_id)
            fired.append(entry.schedule_id)
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            errors.append(str(exc))
    result = {"fired": fired, "errors": errors}
    text = f"{len(fired)} missed reminder(s) fired."
    if errors:
        text += f" {len(errors)} failed."
    return result, text
