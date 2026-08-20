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
        undecided = _count(receipt.get("needs_judgement")) + _count(
            receipt.get("proposals")
        )
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


_DETECTORS: list[Callable[[DetectionContext], list[OperatorAction]]] = [
    _detect_package_update,
    _detect_vault_location,
    _detect_unrehomed_people,
    _detect_vault_vocabulary,
    _detect_unmigrated_links,
    _detect_missed_schedules,
    _detect_review_queue,
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
    raise ValueError(f"unknown operator action id: {action_id}")


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
