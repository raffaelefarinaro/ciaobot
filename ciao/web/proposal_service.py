"""Proposal-queue domain services behind the PWA proposal routes.

The queue is a markdown file per workspace (``Workspace/Memory-Proposals.md``)
plus a folder of skill-reflection notes. These helpers own reading that queue,
rewriting it, and performing the promotion an accept implies — writing a fact
into a bounded region, folding a doc, re-homing a note. The route handlers in
``ciao/web/routes_api.py`` keep request parsing, authorization, and response
mapping and call in here for everything else, so a queue rewrite or a promotion
can be exercised without building a request.
"""

from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

from ciao import proposal_kinds
from ciao import proposal_tracking
from ciao import vault_rehome
from ciao.memory_tool import resolve_region

logger = logging.getLogger(__name__)


# A section header opens with a date (either a plain ``YYYY-MM-DD`` or the
# timestamped ``YYYY-MM-DDThh:mm:ss+00:00`` form the curators append). The date
# is what ``dismiss-older-than`` buckets rows against.
_SECTION_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")
# The queue file lives at this relative path inside each workspace's vault.
_PROPOSALS_REL = ("Workspace", "Memory-Proposals.md")
# Skill-reflection proposals live under this folder, one canonical file per skill.
_SKILL_PROPOSALS_REL = ("Workspace", "Skill-Proposals")


def _proposals_file(config, workspace: str) -> Path:
    """The proposal queue for one workspace, rooted at its vault folder."""
    return Path(config.workspace_vault_root(workspace)).joinpath(*_PROPOSALS_REL)


def _skill_proposals_dir(config, workspace: str) -> Path:
    """The skill-proposal queue folder for one workspace."""
    return Path(config.workspace_vault_root(workspace)).joinpath(*_SKILL_PROPOSALS_REL)


def _sweep_queue_file(
    queue: Path, cutoff, config, workspace: str
) -> dict[str, Any]:
    """Expire dated sections in one queue file, under the queue lock.

    Runs in a worker thread (`asyncio.to_thread`) because `queue_lock` waits
    with a synchronous sleep; doing that on the event loop would freeze every
    other request while another writer holds the lock. Returns the count and,
    when a rewrite landed, the per-fact fields the caller records.
    """
    from ciao.memory_receipts import (
        queue_lock,
        queue_resolution_multi,
        write_queue_atomically,
    )

    result: dict[str, Any] = {
        "removed": 0,
        "changed": False,
        "kinds": [],
        "texts": [],
        "sources": [],
    }
    with queue_lock(queue):
        queue_before = queue.read_text(encoding="utf-8")
        lines = queue_before.splitlines()
        keep: list[str] = []
        section_date = None
        changed = False
        swept_kinds: list[str] = []
        swept_texts: list[str] = []
        swept_sources: list[str] = []
        for raw_line in lines:
            m = _SECTION_DATE_RE.match(raw_line)
            if m:
                section_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                keep.append(raw_line)
                continue
            bullet = proposal_kinds.parse_bullet(raw_line)
            if bullet is not None and section_date is not None and section_date < cutoff:
                changed = True
                swept_kinds.append(bullet.kind)
                swept_texts.append(bullet.text)
                swept_sources.append(bullet.source)
                continue
            keep.append(raw_line)
        if changed:
            try:
                vault_for_receipt = Path(config.workspace_vault_root(workspace))
            except (AttributeError, ValueError):
                vault_for_receipt = queue.parent.parent
            queue_after = "\n".join(keep).rstrip() + "\n"
            # One atomic rewrite, one transaction-level prepared/applied
            # receipt pair (undoable as a whole) plus non-undoable per-fact
            # history rows.
            with queue_resolution_multi(
                queue,
                [
                    {"text": text, "kind": kind, "promoted": False}
                    for kind, text in zip(swept_kinds, swept_texts)
                ],
                actor="operator",
                source="pwa",
                workspace=workspace,
                vault_root=vault_for_receipt,
            ):
                write_queue_atomically(queue, queue_after)
    result["changed"] = changed
    result["removed"] = len(swept_texts)
    result["kinds"] = swept_kinds
    result["texts"] = swept_texts
    result["sources"] = swept_sources
    return result


def _rewrite_queue_single(
    queue: Path,
    line_index: int,
    raw: str,
    removed_text: str,
    kind: str,
    promoted: bool,
    workspace: str,
    vault_root: Path,
) -> bool:
    """Remove one bullet under the queue lock and record its receipt.

    Worker-thread body for the single-proposal route: `queue_lock` waits with a
    synchronous sleep, so this must not run on the event loop. Returns whether
    this call actually removed the bullet.
    """
    from ciao.memory_receipts import (
        queue_lock,
        queue_resolution,
        write_queue_atomically,
    )

    with queue_lock(queue):
        lines = queue.read_text(encoding="utf-8").splitlines()
        removed_ours = _remove_bullet_line(lines, line_index, raw)
        if not removed_ours:
            return False
        with queue_resolution(
            queue,
            removed_text=removed_text,
            kind=kind,
            promoted=promoted,
            actor="operator",
            source="pwa",
            workspace=workspace,
            vault_root=vault_root,
        ) as _receipt:
            queue_after = "\n".join(lines).rstrip() + "\n"
            write_queue_atomically(queue, queue_after)
    return True


def _rewrite_queue_batch(
    queue: Path,
    rows: list[dict[str, Any]],
    keep_lines: set[int],
    removals: list[dict[str, Any]],
    workspace: str,
    vault_root: Path,
) -> set[str]:
    """Remove batch rows under the queue lock and record the transaction.

    Runs in a worker thread: `queue_lock` waits with a synchronous sleep, so
    doing this on the event loop would freeze unrelated requests while another
    writer holds the lock. Returns the ids whose bullets this call removed.
    """
    from ciao.memory_receipts import (
        queue_lock,
        queue_resolution_multi,
        write_queue_atomically,
    )

    with queue_lock(queue):
        lines = queue.read_text(encoding="utf-8").splitlines()
        # Highest index first so the lower ones stay valid, and each removal
        # verifies the content at that index - a promotion above may have
        # awaited a model call while another request rewrote this same file.
        removed_here: set[str] = set()
        for row in sorted(rows, key=lambda r: int(r.get("line", -1)), reverse=True):
            if int(row.get("line", -1)) in keep_lines:
                continue
            if _remove_bullet_line(
                lines, int(row.get("line", -1)), str(row.get("raw") or "")
            ):
                removed_here.add(row["id"])
        queue_after = "\n".join(lines).rstrip() + "\n"
        removed_removals = [
            item
            for item, row in zip(removals, rows)
            if row["id"] in removed_here
        ]
        with queue_resolution_multi(
            queue,
            removed_removals,
            actor="operator",
            source="pwa",
            workspace=workspace,
            vault_root=vault_root,
        ):
            write_queue_atomically(queue, queue_after)
    return removed_here


def _find_bullet_line(lines: list[str], line_index: int, raw: str) -> int | None:
    """Where the bullet *raw* sits now, verifying the index before trusting it.

    `_scan_proposal_rows` captures a line index, and an accept can then await an
    unbounded model call before the queue file is rewritten - with no lock
    anywhere. A second accept or dismiss landing in that window shifts every
    later index, so trusting the index alone addressed an UNRELATED proposal.
    The index is only a hint: the content has to match, otherwise the bullet is
    located by text, and a bullet that is already gone is reported as gone
    rather than as someone else's line.
    """
    wanted = raw.strip()
    if 0 <= line_index < len(lines) and lines[line_index].strip() == wanted:
        return line_index
    for index, line in enumerate(lines):
        if line.strip() == wanted:
            return index
    return None


def _remove_bullet_line(lines: list[str], line_index: int, raw: str) -> bool:
    """Drop the bullet *raw* from *lines*; a bullet already gone is a no-op."""
    found = _find_bullet_line(lines, line_index, raw)
    if found is None:
        return False
    del lines[found]
    return True


def bullets_present(queue: Path, rows: Sequence[tuple[int, str]]) -> set[int]:
    """Which of *rows* (line index, raw bullet) are still queued.

    The revalidation half of the accept guard: an accept promotes into its
    destination first and rewrites the queue last, so between this request's
    scan and its promotion another resolver - the CLI, the undo path, a second
    server process - may already have taken the row. Promoting it anyway folds
    the doc, writes the note or increments the recurrence count a second time
    for a bullet this request will then fail to remove.

    Read under `queue_lock` so it cannot observe a half-written queue, which
    makes it a synchronous wait: callers run it in a worker thread, never on
    the event loop. Returns line indices, matching the `keep_lines` the batch
    rewrite already speaks in.
    """
    from ciao.memory_receipts import queue_lock

    with queue_lock(queue):
        try:
            lines = queue.read_text(encoding="utf-8").splitlines()
        except OSError:
            # No queue file, no bullets: treat every row as already resolved
            # rather than promoting into a destination we cannot then update.
            return set()
    return {
        line for line, raw in rows if _find_bullet_line(lines, line, raw) is not None
    }


# Proposal ids whose promotion is in flight in this process, keyed as
# `_claim_key` builds them, plus the guard that makes test-and-add atomic.
_CLAIMED: set[str] = set()
_CLAIM_GUARD = threading.Lock()


def _claim_key(queue: Path, proposal_id: str) -> str:
    """Identity of one in-flight accept: the queue's lock key plus the row id.

    Derived from `memory_receipts.lock_path_for`, the same resolution
    `queue_lock` uses, so a claim and the file lock name the same queue instead
    of the codebase growing a second notion of queue identity.
    """
    from ciao.memory_receipts import lock_path_for

    try:
        resolved = str(queue.resolve())
    except OSError:
        resolved = str(queue)
    return f"{lock_path_for(resolved)}::{proposal_id}"


@contextmanager
def claim_proposals(queue: Path, ids: Sequence[str]) -> Iterator[set[str]]:
    """Reserve rows so only one in-flight request promotes each of them.

    Two tabs accepting the same proposal both passed the id lookup, both
    promoted - a doc folded twice, a recurrence count incremented twice - and
    only then contended on the queue rewrite, where the loser removed nothing
    and still reported success. The claim is taken BEFORE the first
    destination mutation and held until the rewrite has landed, so the second
    request is turned away instead of promoting again.

    In-process only, and deliberately: the lock the CLI shares is a file lock
    taken with a synchronous wait, and holding it across a promotion - a model
    call, for a fold - would block every other queue writer for its duration
    and, from the event loop, stall the whole server. A resolver in another
    process is caught by `bullets_present` instead, which re-reads the queue
    under that file lock immediately before the promotion.

    Yields the subset of *ids* this call claimed; the rest are in flight
    elsewhere and must not be promoted here.
    """
    keys = {pid: _claim_key(queue, pid) for pid in ids}
    claimed: set[str] = set()
    with _CLAIM_GUARD:
        for pid, key in keys.items():
            if key in _CLAIMED:
                continue
            _CLAIMED.add(key)
            claimed.add(pid)
    try:
        yield claimed
    finally:
        with _CLAIM_GUARD:
            for pid in claimed:
                _CLAIMED.discard(keys[pid])


# A content-derived, stable id for one queued proposal.
#
# The id hashes the bullet's content plus the workspace and file it lives in,
# so dismissing a neighbouring row never renumbers or renames a survivor.
# ``dup`` is the occurrence index among identical bullets inside one file, used
# only to keep two textually identical rows addressable; it is stable because
# it counts only same-file duplicates, which are unaffected by rows in other
# files (or non-duplicate rows in this one) being removed.
#
# Imported rather than redefined: `proposal_tracking.pending_proposal_ids`
# decides whether a resolution helper chat can be archived by comparing ids
# against the ones this module hands out. Two copies that drift apart stop
# matching silently — no error, just helper chats that never archive — so
# there is exactly one implementation.
_stable_proposal_id = proposal_tracking.stable_proposal_id


def _rehome_signal(config) -> dict[str, dict[str, Any]]:
    """Live rehome evidence for every person note, keyed by its queue path.

    Re-computed from the vault rather than trusted from the bullet text: the
    bullet records the destination and reason at queue time, but the UI needs
    to know whether that destination is backed by a tag signal *now*. Keys are
    the vault-relative path forms the bullet names (``personal/People/Mo.md``).
    """
    try:
        candidates = vault_rehome.detect_misfiled_people(
            config.vault_root,
            workspaces=config.workspace_names(),
            # Every vault in the install. Scanning `config.vault_root` returned
            # zero candidates on a migrated install, so the proposals UI silently
            # lost every re-home hint.
            targets=(
                config.vault_scan_targets()
                if hasattr(config, "vault_scan_targets")
                else None
            ),
        )
    except Exception:  # noqa: BLE001 — a broken scan must not fail the list route
        logger.exception("proposal list: rehome signal scan failed")
        return {}
    out: dict[str, dict[str, Any]] = {}
    roles = vault_rehome.resolve_role_workspaces(list(config.workspace_names()))
    for candidate in candidates:
        # Only a single clean signal makes a destination justified; every
        # judgement case the queue holds is explicitly not that.
        justified = candidate.bucket == "mechanical" and bool(candidate.destination)
        # The candidate set is tag-derived, not the guess: a no-tag note names
        # no workspace even though a default counterpart was computed, and a
        # dual-tag note names both. A UI renders these as a picker.
        signalled_roles = {
            vault_rehome.TAG_WORKSPACE_ROLES[t]
            for t in candidate.tags
            if t in vault_rehome.TAG_WORKSPACE_ROLES
        }
        candidate_ws = sorted({roles[role] for role in signalled_roles if role in roles} - {""})
        out[candidate.path] = {
            "destination": candidate.destination,
            "target_workspace": candidate.target_workspace,
            "reason": candidate.reason,
            "justified": justified,
            # Every workspace the tags name is a candidate destination. A
            # dual-tag row yields two, so a UI can render a picker instead of a
            # single pre-filled accept.
            "candidates": candidate_ws,
        }
    return out


def _leak_warning(config, kind: str, workspace: str) -> bool:
    """True when accepting this row would leak a region into the wrong session.

    A ``[memory]`` / ``[profile]`` accept edits one CLAUDE.md region. While one
    guide is shared by every workspace, a proposal queued from another workspace
    and accepted here writes a fact into sessions that did not originate it.
    Region-edit kinds only: a rehome is a file move, not a region write, so it
    never leaks.

    Per-workspace guides have LANDED, which retires this for a migrated install:
    ``_promote_region_row`` resolves the guide through ``agent_root``, so a work
    row is written into work's own ``CLAUDE.md`` and nothing else loads it. The
    condition used to be "not the primary workspace" with the comment "until
    per-workspace guides land", so after the re-rooting it told the operator that
    accepting their own work row would be "visible in every workspace" — of a
    guide only that workspace reads. A warning that is false is worse than none:
    it teaches the operator to click through warnings.
    """
    try:
        accept = proposal_kinds.accept_for(kind)
    except proposal_kinds.UnknownKindError:
        return False
    if accept.action != "edit_region":
        return False
    try:
        shared_guide = Path(config.agent_root(workspace)) == Path(config.workspace_root)
    except (AttributeError, ValueError):
        # No agent_root seam to ask: assume the shared layout, which is the
        # answer that warns rather than the one that stays quiet.
        shared_guide = True
    if not shared_guide:
        return False
    return bool(workspace != config.primary_workspace())


def _perform_rehome_move(config, row: dict[str, Any], target: str) -> dict[str, Any]:
    """Move a queued person note into ``target``, links and all.

    Until now a rehome accept dropped the bullet and moved nothing — the panel
    said so in prose ("Re-home rows are not moved here") and `move_file` was a
    declared accept descriptor that nothing handled. So the queue could ask the
    question and never carry out the answer.

    The row names the note in RENDERED identity form (``personal/People/Mo.md``);
    the mover works install-relative (``personal/memory-vault/People/Mo.md``),
    because that is the space in which a relative link's arithmetic is real. The
    leaf comes from the workspace's own vault directory rather than a constant,
    for the same reason the rebuilds take it.
    """
    from ciao.vault_rehome import move_note_between_roots

    note = str((row.get("rehome") or {}).get("note") or "")
    parts = Path(note).parts
    if len(parts) < 2:
        return {"ok": False, "error": f"the bullet does not name a note ({note!r})"}
    workspace = parts[0]
    try:
        install_root = Path(config.workspace_root)
        vault = Path(config.workspace_vault_root(workspace))
        relative_vault = vault.relative_to(install_root)
        targets = config.vault_scan_targets()
        names = list(config.workspace_names())
    except (AttributeError, ValueError) as exc:
        return {"ok": False, "error": f"could not resolve the vault layout: {exc}"}
    # Derived from the registry, never assumed: the vault sits at
    # `<workspace>/<leaf>` per root and at `<leaf>/<workspace>` while shared. The
    # mover moves a note BETWEEN roots, which only exist in the first shape, so
    # the second is refused with the reason rather than silently building
    # `personal/personal/People/Mo.md` and reporting the note missing.
    vault_parts = relative_vault.parts
    if len(vault_parts) != 2 or vault_parts[0] != workspace:
        return {
            "ok": False,
            "error": (
                f"'{workspace}' does not have its own workspace folder yet "
                f"(its vault is {relative_vault.as_posix()}), so there is no other "
                "root to move a note into"
            ),
        }
    source = (relative_vault / Path(*parts[1:])).as_posix()
    result = move_note_between_roots(
        install_root, source, target, targets=targets, workspaces=names, apply=True
    )
    if result["refusals"]:
        return {"ok": False, "error": result["refusals"][0], "move": result}
    return {
        "ok": True,
        "destination": result["destination"],
        "files_rewritten": result["files_rewritten"],
        "already_moved": bool(result.get("already_moved")),
        "move": result,
    }


def _rehome_target(row: dict[str, Any], requested: str) -> tuple[str, str]:
    """The workspace a rehome accept should move into, or an error.

    An explicit request wins, because a row whose tags name two workspaces is a
    question only the operator can answer. Otherwise the destination has to be
    backed by a single clean tag signal — accepting an unjustified guess would
    move somebody's note on the strength of nothing.
    """
    signal = row.get("rehome") or {}
    if requested:
        # Any registered workspace, not only the ones the tags name. The tags are
        # a hint; the operator asking is the authority, and most queued rows have
        # no tag naming anywhere — restricting the choice to tag-named candidates
        # left every one of the reference install's fourteen rows unmovable, which
        # is the complaint that started this. `move_note_between_roots` still
        # refuses an unregistered name.
        return requested, ""
    if not signal.get("justified"):
        return "", "no tag backs a destination for this note, so pick one explicitly"
    destination = str(signal.get("destination") or "")
    target = Path(destination).parts[0] if destination else ""
    if not target:
        return "", "the signal names no destination workspace"
    return target, ""


def _scan_proposal_rows(config) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Scan every workspace's proposal queue and skill-proposal folder.

    Returns (rows, by_id) where ``by_id`` maps a stable id to the file context
    needed to remove that row later (workspace, absolute path, line index). Each
    row carries the queue fields plus kind-specific signal: a rehome exposes
    candidate destinations and whether any is justified, and a region accept
    from a foreign workspace carries the leak warning.
    """
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    rehome = _rehome_signal(config)
    ws_names = list(config.workspace_names())

    for workspace in config.workspace_names():
        queue = _proposals_file(config, workspace)
        rel_path = Path(workspace).joinpath(*_PROPOSALS_REL).as_posix()
        if queue.is_file():
            # The same walk `proposal_tracking.pending_proposal_ids` uses, so
            # the ids the review tab hands out and the ids the archive check
            # looks for can never drift apart.
            for entry in proposal_tracking.walk_proposal_queue(
                workspace, rel_path, queue.read_text(encoding="utf-8")
            ):
                line_index, raw, bullet, pid = (
                    entry.line, entry.raw, entry.bullet, entry.proposal_id,
                )
                row: dict[str, Any] = {
                    "id": pid,
                    "kind": bullet.kind,
                    "text": bullet.text,
                    "source": bullet.source,
                    "workspace": workspace,
                    "path": rel_path,
                    "line": line_index,
                    # The line as read. The index alone is not enough to delete
                    # by: an accept can await a model call, and a concurrent
                    # accept/dismiss rewrites the file underneath it.
                    "raw": raw,
                }
                if bullet.target:
                    # The payload a destination kind acts on: the person name
                    # for [people], the doc path for [project]. Region kinds
                    # and rehome carry none.
                    row["target"] = bullet.target
                accept = proposal_kinds.accept_for(bullet.kind)
                if accept.action == "edit_region":
                    row["region"] = resolve_region(bullet.kind)
                    row["leak_warning"] = _leak_warning(config, bullet.kind, workspace)
                elif accept.action == "move_file":
                    # Rehome rows: expose the live signal. The destination named
                    # in the bullet is a guess unless the tags justify it, and a
                    # dual-tag note names more than one candidate.
                    signal = _rehome_lookup(rehome, bullet.text, ws_names)
                    row["rehome"] = {
                        # The note this row is about, so a UI can show a name and
                        # a direction instead of reprinting the whole bullet.
                        "note": signal["note"],
                        "destination": signal["destination"],
                        "candidates": signal["candidates"],
                        "justified": signal["justified"],
                        # Whether the bullet outlived its cause. Dropping this
                        # field made a stale row render identically to a
                        # genuine "needs a decision" one in the PWA, which is
                        # exactly what `_rehome_lookup` computes it to prevent.
                        "stale": bool(signal.get("stale")),
                        "reason": signal["reason"],
                    }
                rows.append(row)
                by_id[pid] = {
                    "workspace": workspace,
                    "path": str(queue),
                    "line": line_index,
                    "row": row,
                }
        # Skill proposals are files, not bullets: no parse_bullet, no accept
        # descriptor, and a whole file is the atomic unit.
        #
        # They are registered in `by_id` all the same, with `file: True` so the
        # handlers can tell a file from a bullet. Listing them without
        # registering them left the read surface working and the write surface
        # missing: the UI renders a dismiss button per row, and every one of the
        # 49 skill rows on a real vault answered 404 "unknown proposal id" —
        # from both the single-row and the batch endpoint. A row you cannot act
        # on is a notification wearing a button.
        skill_dir = _skill_proposals_dir(config, workspace)
        if skill_dir.is_dir():
            for f in sorted(skill_dir.glob("*.md")):
                row_id = _stable_proposal_id(workspace, rel_path, "skill", f.name, "", 0)
                row = {
                    "id": row_id,
                    "kind": "skill",
                    "text": f.stem,
                    "source": "",
                    "workspace": workspace,
                    "path": Path(workspace).joinpath(*_SKILL_PROPOSALS_REL, f.name).as_posix(),
                    "line": -1,
                }
                rows.append(row)
                by_id[row_id] = {
                    "workspace": workspace,
                    "path": str(f),
                    "line": -1,
                    "row": row,
                    "file": True,
                }
    return rows, by_id


def _dismiss_skill_proposal(ctx: dict[str, Any]) -> dict[str, Any]:
    """Take one skill-proposal FILE out of the queue.

    A reviewed proposal is a resolved decision: whether it was implemented or
    disregarded, keeping the file in the queue re-asks the same question. So
    dismiss deletes it rather than moving it aside — the queue is globbed one
    level deep, so either clears it, and the decision is the operator's to keep
    in the Curation-Log. A missing file is already gone, not an error.
    """
    source = Path(ctx["path"])
    if not source.is_file():
        return {"ok": True, "deleted": True}
    try:
        source.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"could not delete {source.name}: {exc}"}
    return {"ok": True, "deleted": True}


def _rehome_lookup(
    rehome: dict[str, dict[str, Any]], text: str, workspaces: Sequence[str] = ()
) -> dict[str, Any]:
    """Resolve a rehome bullet's live signal from its named path.

    The bullet names the source path in backticks (``personal/People/Mo.md``);
    pull that out and match it against the scan keyed by path.

    The alternation is built from the REGISTERED workspace names rather than
    hardcoding ``personal|work``: a workspace named anything else never matched,
    so its rows silently showed "no live rehome signal" forever. Escaped, because
    a workspace name is the user's and may contain regex metacharacters.
    """
    names = [re.escape(n) for n in workspaces if n] or [r"[^/`]+"]
    m = re.search(rf"`((?:{'|'.join(names)})/[^`]+\.md)`", text)
    path = m.group(1) if m else ""
    signal = rehome.get(path)
    if signal is None:
        # The bullet outlived its cause: the note was tagged, moved, or a later
        # rule settled it, and nothing re-detects it now. Marked `stale` rather
        # than left looking undecided — the queue rendered it identically to a
        # genuine "needs a decision" row, so the operator could not tell which
        # rows were asking them something and which were just litter. Two of the
        # reference install's fourteen are in this state.
        return {
            "note": path,
            "destination": "",
            "candidates": [],
            "justified": False,
            "stale": True,
            "reason": "no live rehome signal for this note",
        }
    return {"note": path, "stale": False, **signal}


def _resolve_batch(config, ids: list[str]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Map ids to removable file contexts, or return an error.

    Returns (None, error) on the first unknown id: the whole batch must resolve
    before anything is written, so an unknown id aborts the batch without
    touching any file.
    """
    _, by_id = _scan_proposal_rows(config)
    resolved: list[dict[str, Any]] = []
    for pid in ids:
        ctx = by_id.get(pid)
        if ctx is None:
            return None, f"unknown proposal id: {pid}"
        resolved.append(ctx)
    return resolved, None


def _promote_region_row(config, row: dict[str, Any]) -> dict[str, Any]:
    """Write an accepted memory/profile fact into its workspace's region.

    Accept used to remove the bullet and return a descriptor saying what SHOULD
    happen, matching the MCP flow where the agent edits and then dismisses. In a
    UI where a person clicks Accept that meant the fact left the queue and landed
    nowhere — one click from losing it.

    Order is write-then-dismiss, never the reverse, which is the same rule the
    curation prompt states: the reverse loses the fact if anything fails between
    the two steps. So this returns a failure and the caller keeps the bullet.

    Goes through ``accept_region_fact`` rather than ``update_region`` directly,
    so a click gets what an archive-time promotion gets: the event-shape guard,
    the stamp-stripped duplicate check, the learned-at stamp the aging audit
    reads, and the consolidations undo log. It takes the same guide lock
    ``update_region`` did.

    The region cap stays ADVISORY, as `update_region` documents: enforcing it
    made the accept button dead for 67 of 130 queued proposals on a real vault.
    Usage is reported, never used to refuse.

    The guide is resolved through ``agent_root``, so before the re-rooting this
    writes the shared guide (and the row's ``leak_warning`` is why the UI asks
    for confirmation first) and afterwards that workspace's own.
    """
    from ciao.memory_proposals import accept_region_fact
    from ciao.memory_tool import ensure_regions, memory_status, resolve_region as _resolve

    region = _resolve(row.get("region") or row["kind"])
    guide = Path(config.agent_root(row["workspace"])) / "CLAUDE.md"
    try:
        # A guide with no region markers yet is not a reason to refuse a
        # promotion — a workspace can be newer than its last skill sync. This is
        # the same call sync makes, and it is a no-op once the markers are there.
        ensure_regions(guide)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not prepare {guide}: {exc}", "region": region}

    try:
        vault_root = Path(config.workspace_vault_root(row["workspace"]))
    except (AttributeError, ValueError):
        # Only the undo log needs it; a promotion must not fail for want of one.
        vault_root = None

    try:
        outcome, promotable = accept_region_fact(
            guide_path=guide,
            target=row.get("region") or row["kind"],
            text=row["text"],
            vault_root=vault_root,
            actor="operator",
            source="pwa",
            workspace=str(row.get("workspace") or ""),
        )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc), "region": region}

    def _usage() -> dict[str, Any]:
        try:
            status = memory_status(
                guide,
                memory_char_limit=int(getattr(config, "memory_char_limit", 3000)),
                user_char_limit=int(getattr(config, "user_char_limit", 1375)),
            )
        except Exception:  # noqa: BLE001 — usage is advisory reporting only
            return {}
        if not isinstance(status, dict):
            return {}
        entry = status.get(region, {})
        return dict(entry) if isinstance(entry, dict) else {}

    if outcome == "written":
        # `written` is reported because the guard can promote only the trailing
        # durable-rule clause of a bullet, so what landed is not always the
        # sentence the operator read on the row.
        return {"ok": True, "region": region, "written": promotable, "usage": _usage()}
    if outcome == "duplicate":
        # Already remembered. The fact is in the region either way, so the row
        # is resolved and may leave the queue.
        return {"ok": True, "region": region, "duplicate": True, "usage": _usage()}
    if outcome == "unshaped":
        # Event-shaped text is exactly what the region must not hold; this used
        # to be written verbatim. The row stays queued.
        return {
            "ok": False,
            "region": region,
            "error": (
                "this reads as an event, not a standing rule, so it would rot "
                "in always-loaded memory. Use \u201ctalk about it\u201d to rephrase it as "
                "what is true from now on, then accept."
            ),
        }
    if outcome == "conflict":
        # The region changed under a concurrent writer between planning and
        # write. Nothing was written; the row survives and a retry re-reads.
        return {
            "ok": False,
            "region": region,
            "conflict": True,
            "error": (
                f"ciao:{region} changed while this was being applied, so nothing "
                "was written. Retry to apply it against the current entries."
            ),
        }
    return {"ok": False, "region": region, "error": f"could not write ciao:{region}"}


def _accept_people_row(config, row: dict[str, Any]) -> dict[str, Any]:
    """Write an accepted `[people]` fact into a stub person note.

    A note that already exists is not appended to blindly — merging a new fact
    into someone's curated note is a judgment call, so the row stays queued
    and the error says so.
    """
    from ciao.memory_proposals import write_people_note

    name = str(row.get("target") or "").strip()
    if not name:
        return {"ok": False, "error": "the bullet names no person"}
    try:
        vault = config.workspace_vault_root(row["workspace"])
    except (AttributeError, ValueError) as exc:
        return {"ok": False, "error": f"could not resolve the vault: {exc}"}
    try:
        created = write_people_note(Path(vault), name, row["text"])
    except OSError as exc:
        return {"ok": False, "error": f"could not write the note: {exc}"}
    if not created:
        return {
            "ok": False,
            "error": f"People/{name}.md already exists; merge the fact manually, then dismiss",
        }
    return {"ok": True, "destination": f"People/{name}.md"}


def _accept_learnings_row(config, row: dict[str, Any]) -> dict[str, Any]:
    """Append an accepted `[learnings]` fact to Workspace/Learnings.md."""
    from ciao.memory_proposals import append_learning

    try:
        vault = config.workspace_vault_root(row["workspace"])
    except (AttributeError, ValueError) as exc:
        return {"ok": False, "error": f"could not resolve the vault: {exc}"}
    try:
        append_learning(Path(vault), row["text"])
    except OSError as exc:
        return {"ok": False, "error": f"could not append the learning: {exc}"}
    return {"ok": True, "destination": "Workspace/Learnings.md"}


async def _accept_project_row(config, row: dict[str, Any]) -> dict[str, Any]:
    """Fold an accepted `[project]` bullet into its canonical doc.

    Reuses the archive-time fold (guards, NO_CHANGES sentinel, per-doc lock)
    with just this bullet as input. ``False`` back means the model judged the
    doc already covers the fact or a guard rejected the rewrite — ambiguous
    enough that dropping the row silently would be wrong, so the caller keeps
    it queued and the operator decides.
    """
    from ciao.project_doc_update import update_project_doc

    doc_raw = str(row.get("target") or "").strip()
    if not doc_raw:
        return {"ok": False, "error": "the bullet names no project doc"}
    doc = Path(doc_raw)
    if not doc.is_absolute():
        # Same resolution the archive-time fold uses: workspace-root-relative.
        doc = Path(config.workspace_root) / doc
    if not doc.is_file():
        return {"ok": False, "error": f"project doc not found: {doc_raw}"}
    insights = f"## Decisions\n- {row['text']}\n"
    try:
        wrote = await update_project_doc(
            doc_path=doc,
            insights_md=insights,
            model=getattr(config, "insights_model", "") or "sonnet",
        )
    except Exception as exc:  # noqa: BLE001 — a failed fold keeps the row
        return {"ok": False, "error": f"fold failed: {exc}"}
    if not wrote:
        return {
            "ok": False,
            "error": "the fold reported no changes; dismiss instead if the doc already covers this",
        }
    return {"ok": True, "destination": doc_raw}


def _decision_destination(accept_action: str, row: dict[str, Any], outcome: dict[str, Any]) -> str:
    """Where an accepted row's fact landed, for the decision history's benefit.

    Mirrors the per-branch destination each accept helper already knows, so
    the history ledger and the response payload agree without a second
    source of truth. Rehome rows: nothing is written here (the move is
    performed above the queue-file grouping), so ``outcome`` carries the
    move's own ``destination``.
    """
    if accept_action == "edit_region":
        region = outcome.get("region") or row.get("region") or row.get("kind", "")
        return f"ciao:{region}" if region else ""
    if accept_action == "move_file":
        return str(outcome.get("destination", ""))
    # fold_doc, write_people_note, append_learnings all set "destination".
    return str(outcome.get("destination", ""))
