"""Plan and apply the per-workspace agent-root migration.

Today one install directory holds one vault with every workspace nested inside
it, and a single ``CLAUDE.md``, ``.mcp.json`` and ``.claude/`` shared by all of
them. The destination gives each registered workspace its own agent root, a
sibling of the global layer rather than a child of it::

    <install>/
      .runtime/           global: credentials, schedules, job runs, state, FTS
      Logs/               derived transcripts, stays global (D5)
      templates-src/      the shared templates, mirror source (D5)
      personal/           an agent root
        memory-vault/
      work/               same shape, its own everything

This module owns the planning half and the receipt. Planning is READ ONLY: it
classifies every path under the vault and refuses rather than guessing. The
apply half is gated on the verification suite, because a migration that can stop
halfway is worse than one that refuses outright.

Classification is exhaustive on purpose. Every path lands in exactly one of
``moves``, ``global_keeps``, ``regenerated`` or ``ignored``, and anything left
over goes to
``unclassified``, which makes the plan refuse. A migration that silently skips a
file it did not recognise is how a vault loses notes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.vault_index import EXCLUDED_TOP_DIRS

RECEIPT_VERSION = 1

# D5. Logs/ holds roughly 71% of all notes, is derived output rather than
# curated content, and 1454 chat ids cannot each be resolved back to one
# workspace. Promoted to <install>/Logs/ unmoved, which removes the bulk of the
# migration's file-count risk. Templates/ becomes the mirror source.
_GLOBAL_PROMOTIONS: dict[str, str] = {
    "Logs": "Logs",
    "Templates": "templates-src",
    # Editor state for the vault as a whole, not for any one workspace. Promoted
    # beside the workspaces rather than duplicated into each root. Promoting it
    # rather than leaving it behind is what lets the vault directory end up empty
    # and be removed, instead of lingering as a vestigial shell.
    ".obsidian": ".obsidian",
}

# Generated aggregates that a per-root rebuild replaces (P10.6). They are not
# moved and not preserved: an index that still describes the old prefixed layout
# is worse than an absent one, because it reads as current.
_REGENERATED_ROOT_NOTES: frozenset[str] = frozenset(
    {"INDEX.md", "MEMORY.md", "VOCABULARY.md"}
)

# Filesystem cruft that carries no vault content. Listed explicitly rather than
# pattern-matched, so a genuinely unrecognised dotfile still refuses the plan.
# The reference vault has a .DS_Store at its root, which the census does not
# report because it counts loose .md files and per-directory non-.md files, and
# a loose non-markdown file at the vault root falls between those two.
_IGNORABLE_FILES: frozenset[str] = frozenset({".DS_Store"})


@dataclass(frozen=True, slots=True)
class Move:
    """One source-to-destination move, both relative to the install root."""

    source: str
    destination: str
    workspace: str


@dataclass(slots=True)
class RerootPlan:
    """What the migration would do, and why it would refuse."""

    install_root: str
    vault_root: str
    workspaces: list[str] = field(default_factory=list)
    moves: list[Move] = field(default_factory=list)
    global_keeps: list[str] = field(default_factory=list)
    regenerated: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.refusals or self.unclassified)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_VERSION,
            "install_root": self.install_root,
            "vault_root": self.vault_root,
            "workspaces": list(self.workspaces),
            "moves": [
                {"source": m.source, "destination": m.destination, "workspace": m.workspace}
                for m in self.moves
            ],
            "global_keeps": list(self.global_keeps),
            "regenerated": list(self.regenerated),
            "ignored": list(self.ignored),
            "unclassified": list(self.unclassified),
            "refusals": list(self.refusals),
            "refused": self.refused,
        }


def plan(install_root: Path, vault_root: Path, workspaces: list[str]) -> RerootPlan:
    """Classify every top-level vault path into its destination. Never writes.

    Refuses, rather than guessing, when: the vault is missing, no workspace is
    registered, a registered workspace has no vault directory, a destination is
    already a non-empty directory, or any path is left unclassified.
    """
    install_root = Path(install_root).resolve()
    vault_root = Path(vault_root).resolve()
    result = RerootPlan(install_root=str(install_root), vault_root=str(vault_root))
    result.workspaces = sorted(workspaces)

    if not vault_root.is_dir():
        result.refusals.append(f"vault root is not a directory: {vault_root}")
        return result
    if not result.workspaces:
        result.refusals.append("no registered workspace, so there is no root to create")
        return result

    registered = set(result.workspaces)
    for name in result.workspaces:
        # A root with no vault is reported, not guessed (P10.11).
        if not (vault_root / name).is_dir():
            result.refusals.append(
                f"workspace '{name}' is registered but has no vault directory at "
                f"{vault_root / name}"
            )
        destination = install_root / name
        if destination.is_dir() and any(destination.iterdir()):
            result.refusals.append(
                f"destination for '{name}' already exists and is not empty: {destination}"
            )

    for entry in sorted(vault_root.iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_symlink():
            # A symlink can point outside the vault, so moving it is not a
            # file-count-preserving operation and needs a human decision.
            result.unclassified.append(f"{name} (symlink)")
            continue
        if entry.is_dir():
            if name in registered:
                result.moves.append(
                    Move(
                        source=f"{vault_root.name}/{name}",
                        destination=f"{name}/{vault_root.name}",
                        workspace=name,
                    )
                )
            elif name in _GLOBAL_PROMOTIONS:
                result.moves.append(
                    Move(
                        source=f"{vault_root.name}/{name}",
                        destination=_GLOBAL_PROMOTIONS[name],
                        workspace="",
                    )
                )
            else:
                result.unclassified.append(f"{name} (unregistered directory)")
            continue
        if name in _REGENERATED_ROOT_NOTES:
            result.regenerated.append(f"{vault_root.name}/{name}")
        elif name in _IGNORABLE_FILES:
            result.ignored.append(f"{vault_root.name}/{name}")
        else:
            result.unclassified.append(f"{name} (loose file)")

    return result


def receipt_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "migration" / "workspace-rooting.json"


def read_receipt(runtime_root: Path) -> dict[str, Any] | None:
    """The receipt of a COMPLETED re-rooting, or None.

    Gates on ``status == "migrated"``, never on the file existing. A surveyed or
    refused receipt is a record of work still to do, and treating it as done
    would permanently block the run that fixes the install. This is the same
    defect D4 found in the earlier migrations.
    """
    path = receipt_path(runtime_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data if data.get("status") == "migrated" else None


def write_receipt(runtime_root: Path, payload: dict[str, Any]) -> Path:
    """Persist a receipt atomically, keeping any earlier one beside it."""
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def rehearse(
    install_root: Path,
    vault_root: Path,
    workspaces: list[str],
    runtime_root: Path,
) -> dict[str, Any]:
    """Plan and record the result without moving anything.

    Writes ``status: "surveyed"`` or ``status: "refused"``, never "migrated", so
    a rehearsal can never make the real migration look already done.
    """
    result = plan(install_root, vault_root, workspaces)
    payload = result.as_dict()
    payload["status"] = "refused" if result.refused else "surveyed"
    payload["recorded_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload["receipt_path"] = str(write_receipt(runtime_root, payload))
    return payload


def _run_git(root: Path, *args: str) -> tuple[int, str]:
    """Run one git command in ``root``, returning (exit code, combined output)."""
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    # rstrip only. `git status --porcelain` encodes the index and worktree state
    # in the first two columns, so a leading space is significant: stripping it
    # shifts every subsequent slice and silently truncates the first path.
    return proc.returncode, (proc.stdout + proc.stderr).rstrip()


def dirty_tracked_paths(install_root: Path, *relatives: str) -> list[str]:
    """Tracked files under ``relatives`` with staged or unstaged modifications.

    Scoped to TRACKED changes on purpose (P10.2). The reference install carries
    roughly 700 untracked ``Logs/Chats/chat-*`` directories at any moment, so a
    strict whole-tree gate would refuse on every real install and nothing would
    ever migrate. What the gate protects is ``git checkout`` staying a working
    undo for content that is under version control; an untracked chat log has no
    committed state to lose.

    Every path the migration moves has to be covered, not just the vault. The
    skill catalog moves too (P10.5), and a tracked file carried across with
    uncommitted edits is precisely the case where ``git checkout`` stops being a
    working undo.
    """
    paths = [
        relative for relative in relatives if relative and (Path(install_root) / relative).exists()
    ]
    if not paths:
        return []
    code, out = _run_git(
        install_root, "status", "--porcelain", "--untracked-files=no", "--", *paths
    )
    if code != 0 or not out:
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


_REGENERATED_BACKUP = "migration/reroot-regenerated"


def apply(
    install_root: Path,
    vault_root: Path,
    workspaces: list[str],
    runtime_root: Path,
    *,
    primary: str,
) -> dict[str, Any]:
    """Re-root every registered workspace, or none of them.

    ``primary`` is required rather than derived. Which root inherits the shared
    guide's regions and the whole skill catalog is the single most consequential
    choice in the migration, and a caller that has not decided it must not be
    given a default that looks like a decision.

    Every move goes through ``git mv`` so history follows the file, which is what
    makes ``git log --follow`` still work afterwards and what keeps the operation
    a move rather than a copy-and-delete.

    Refuses before touching anything when the plan refuses or when a tracked file
    under the vault has uncommitted modifications. Refusing outright is the whole
    design: a half-rooted install has no filter over a still-prefixed index, so
    every entity would be visible in every session, which is strictly worse than
    not migrating at all.

    The generated root notes are not deleted but moved into the runtime directory,
    so ``undo`` can restore a byte-identical tree. A rebuild replaces them, but
    "the migration is exactly undoable" is worth more than saving three files.
    """
    install_root = Path(install_root).resolve()
    vault_root = Path(vault_root).resolve()
    runtime_root = Path(runtime_root)

    result = plan(install_root, vault_root, workspaces)
    triage = plan_skills_triage(install_root, primary)
    payload = result.as_dict()
    payload["primary"] = primary
    payload["skills_triage"] = triage.as_dict()
    payload["recorded_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if result.refused or triage.refusals:
        payload["status"] = "refused"
        payload["refused"] = True
        payload["refusals"] = [*result.refusals, *triage.refusals]
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload
    if primary not in result.workspaces:
        payload["status"] = "refused"
        payload["refused"] = True
        payload["refusals"] = [
            f"primary workspace '{primary}' is not registered, so the guide "
            "regions and the skill catalog have nowhere to go"
        ]
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload

    moved_relatives = [vault_root.name, *(m.source for m in triage.moves)]
    dirty = dirty_tracked_paths(install_root, *moved_relatives)
    if dirty:
        payload["status"] = "refused"
        payload["refusals"] = [
            f"{len(dirty)} tracked file(s) in "
            f"{', '.join(moved_relatives)} have uncommitted changes; commit or "
            "stash them so git checkout stays a working undo"
        ]
        payload["dirty_tracked"] = dirty[:20]
        payload["refused"] = True
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload

    code, head = _run_git(install_root, "rev-parse", "HEAD")
    payload["git_head_before"] = head if code == 0 else ""

    applied: list[dict[str, str]] = []
    # The skill catalog moves through the same loop as the vaults, so one failure
    # rolls back the whole run and one receipt reverses it. A catalog left behind
    # by a successful vault migration would be a half-rooted install by another
    # name: the primary root would load no custom skill at all.
    for move in [*result.moves, *triage.moves]:
        source = install_root / move.source
        destination = install_root / move.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", move.source, move.destination)
        if code != 0:
            # Roll back what this run already moved, so the install is never left
            # half-rooted, then record why it stopped.
            for done in reversed(applied):
                _run_git(install_root, "mv", done["destination"], done["source"])
            payload["status"] = "refused"
            payload["refused"] = True
            payload["refusals"] = [f"git mv failed for {move.source}: {out}"]
            payload["receipt_path"] = str(write_receipt(runtime_root, payload))
            return payload
        applied.append({"source": move.source, "destination": move.destination})

    # Both the generated notes and the ignorable cruft are STASHED, not deleted.
    # Undo has to restore a byte-identical tree with no caveats, and recreating a
    # sidecar file empty is not identical. Three notes and a Finder sidecar cost
    # nothing to keep, and an exactly-undoable migration is the whole argument for
    # rewriting a user's layout at all.
    backup_dir = runtime_root / _REGENERATED_BACKUP
    backup_dir.mkdir(parents=True, exist_ok=True)
    stashed: list[dict[str, str]] = []
    for relative in [*result.regenerated, *result.ignored]:
        source = install_root / relative
        if not source.is_file():
            continue
        target = backup_dir / Path(relative).name
        source.replace(target)
        stashed.append({"source": relative, "backup": str(target.relative_to(runtime_root))})

    # The vault directory is empty now. Remove it so the layout has exactly one
    # home per workspace, and record that undo recreates it.
    removed_vault = False
    if vault_root.is_dir() and not any(vault_root.iterdir()):
        vault_root.rmdir()
        removed_vault = True

    # Move the registry with the files. Without this the install is left broken:
    # every entry still names memory-vault/<name> while the directory now lives
    # at <name>/memory-vault, so the vault resolves to a path that is gone. The
    # before-image goes in the receipt so undo restores it exactly.
    registry_before, registry_after = _rewrite_registry(runtime_root, result)
    payload["registry_before"] = registry_before
    payload["registry_after"] = registry_after

    primary_vault = next(
        (m.destination for m in result.moves if m.workspace == primary), ""
    )
    payload["created_files"] = write_skills_triage(
        install_root, triage, result.workspaces, primary_vault
    )

    payload["status"] = "migrated"
    payload["applied"] = applied
    payload["stashed_files"] = stashed
    payload["removed_vault_dir"] = removed_vault
    payload["receipt_path"] = str(write_receipt(runtime_root, payload))
    # The receipt is what flips CiaoConfig.agent_root, so the cached answer from
    # before the migration is now stale in this process.
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    return payload


def undo(install_root: Path, runtime_root: Path) -> dict[str, Any]:
    """Reverse a completed re-rooting to a byte-identical tree.

    CLI only. Reverting the architecture is not a housekeeping button, and the
    only reason it exists is so the forward migration is provably exact rather
    than merely tested.

    "Byte-identical" covers what the migration MOVED and CREATED. It does not
    cover the derived rebuilds that run after it: ``rebuild_indexes`` rewrites
    each root's ``INDEX.md`` without the path prefix and writes a per-root
    ``VOCABULARY.md``, and undoing leaves those in the restored vault. On the
    reference install that is exactly two modified and two new files, all of them
    regenerated aggregates, and ``git status`` names them. Un-deriving them here
    would mean the receipt carrying a copy of every index it replaced, which buys
    nothing: they are rebuilt from the notes on the next sync either way.
    """
    install_root = Path(install_root).resolve()
    runtime_root = Path(runtime_root)
    receipt = read_receipt(runtime_root)
    if receipt is None:
        return {"status": "nothing_to_undo", "reason": "no migrated receipt"}

    vault_name = Path(receipt.get("vault_root", "")).name or "memory-vault"
    if receipt.get("removed_vault_dir"):
        (install_root / vault_name).mkdir(parents=True, exist_ok=True)

    restored: list[str] = []
    for entry in receipt.get("stashed_files", []):
        backup = runtime_root / entry["backup"]
        target = install_root / entry["source"]
        if backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            backup.replace(target)
            restored.append(entry["source"])

    # Files the migration CREATED are removed before the moves are reversed,
    # because they live inside the directories about to move back. Removed by
    # recorded path rather than by pattern, so a file the user wrote into the
    # same folder afterwards is never touched.
    removed: list[str] = []
    for relative in receipt.get("created_files", []):
        target = install_root / relative
        if target.is_file():
            target.unlink()
            removed.append(relative)
            _prune_empty_parents(install_root, target.parent)
    source_dir = install_root / _SKILLS_SRC
    if source_dir.is_dir() and not any(source_dir.iterdir()):
        source_dir.rmdir()

    reversed_moves: list[str] = []
    for entry in reversed(receipt.get("applied", [])):
        source = install_root / entry["source"]
        source.parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", entry["destination"], entry["source"])
        if code != 0:
            return {
                "status": "failed",
                "reason": f"git mv failed reversing {entry['destination']}: {out}",
                "reversed": reversed_moves,
            }
        reversed_moves.append(entry["source"])
        # Drop the now-empty root directory the migration created.
        parent = (install_root / entry["destination"]).parent
        if parent != install_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    before = receipt.get("registry_before")
    if before is not None:
        _write_registry(runtime_root, before)

    remove_receipt(runtime_root)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    return {
        "status": "undone",
        "reversed": reversed_moves,
        "restored_stashed": restored,
        "removed_created": removed,
    }


def _prune_empty_parents(install_root: Path, directory: Path) -> None:
    """Drop directories the migration created and then emptied.

    Without this, undoing leaves an empty ``Workspace/`` inside the vault it
    moves back. No file changes, so a hash comparison still passes — which is
    exactly why it has to be handled here rather than caught by the round-trip
    test.
    """
    install_root = Path(install_root).resolve()
    current = directory.resolve()
    while current != install_root and install_root in current.parents:
        if not current.is_dir() or any(current.iterdir()):
            return
        current.rmdir()
        current = current.parent


def remove_receipt(runtime_root: Path) -> bool:
    """Drop the receipt so a later run is not gated by a reverted migration."""
    path = receipt_path(runtime_root)
    if path.is_file():
        path.unlink()
        return True
    return False


def registry_file(runtime_root: Path) -> Path:
    return Path(runtime_root) / "workspaces.json"


def _write_registry(runtime_root: Path, entries: list[dict[str, Any]]) -> None:
    """Persist the registry atomically, preserving key order and unknown keys."""
    path = registry_file(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _rewrite_registry(
    runtime_root: Path, result: RerootPlan
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """Point every migrated workspace's ``vault_root`` at its new agent root.

    Returns ``(before, after)`` so the receipt carries an exact before-image for
    undo. Reads and rewrites the raw list rather than round-tripping through
    WorkspaceConfig, because an unknown key a future release adds must survive a
    migration that only means to change one field.
    """
    path = registry_file(runtime_root)
    if not path.is_file():
        return None, None
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None, None
    if not isinstance(entries, list):
        return None, None

    before = json.loads(json.dumps(entries))
    destinations = {m.workspace: m.destination for m in result.moves if m.workspace}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        destination = destinations.get(str(entry.get("name", "")))
        if destination:
            entry["vault_root"] = destination
    _write_registry(runtime_root, entries)
    return before, entries


# -- P10.4: splitting the shared guide --------------------------------------


@dataclass(frozen=True, slots=True)
class GuideSplit:
    """What each root's ``CLAUDE.md`` gets, and what gets queued for review."""

    primary: str
    per_root: dict[str, str]
    queued: dict[str, list[str]]


def split_guide(guide_text: str, workspaces: list[str], primary: str) -> GuideSplit:
    """Give every root the same body, and the bounded regions to the primary only.

    There is exactly one ``CLAUDE.md`` today, so its bounded regions mix facts
    from every workspace: accepting a work proposal has been writing a region
    that every personal session then loads. The split cannot be automated past
    this point. Deciding which of those entries belongs to which workspace is a
    judgement about the user's own prose, and a heuristic that guesses would
    reproduce exactly the misfiling this release exists to repair, in a place
    that is read before the user has said a word.

    So: the unbounded body is copied verbatim to every root, because standing
    directives apply everywhere. The regions stay with the primary root, whose
    sessions already behave as they do today. Every other root gets the same
    body with EMPTY regions, and the primary's entries are handed to that root's
    proposal queue, where a human accepts the ones that belong. Nothing is lost
    and nothing is guessed.
    """
    from ciao.memory_tool import REGIONS, _REGION_META, parse_entries, read_region  # noqa: PLC0415

    body = strip_region_blocks_text(guide_text)
    region_entries: dict[str, list[str]] = {}
    for region in REGIONS:
        entries, _diagnostics = read_region_text(guide_text, region)
        region_entries[region] = entries

    empty_blocks: list[str] = []
    for region in REGIONS:
        meta = _REGION_META[region]
        empty_blocks.append(f"{meta['start']}\n{meta['end']}")
    empty_regions = "\n\n".join(empty_blocks)

    secondary_text = body.rstrip() + "\n\n" + empty_regions + "\n"
    per_root: dict[str, str] = {}
    queued: dict[str, list[str]] = {}
    for name in workspaces:
        if name == primary:
            per_root[name] = guide_text
            continue
        per_root[name] = secondary_text
        bullets: list[str] = []
        for region in REGIONS:
            for entry in region_entries[region]:
                bullet = _queue_bullet(region, entry)
                if bullet:
                    bullets.append(bullet)
        queued[name] = bullets
    return GuideSplit(primary=primary, per_root=per_root, queued=queued)


_QUEUE_SOURCE = "shared CLAUDE.md before re-rooting"


def _queue_bullet(region: str, entry: str) -> str:
    """One region entry as a single-line proposal bullet, or "" if it is scaffolding.

    Two things have to be normalised, and both were found in the real guide.

    A region body opens with its own markdown heading (``## Agent memory``),
    which `parse_entries` keeps attached to the first entry because entries are
    separated by `§` and a heading is not one. That heading is region scaffolding
    rather than a remembered fact, so it is dropped.

    Entries may span multiple lines. The queue's invariant is one bullet per
    line: `proposal_kinds.BULLET_RE` matches line by line, so a multi-line bullet
    would leave its continuation lines as loose prose in Memory-Proposals.md,
    uncountable by every counter and invisible to the dedupe check. Newlines are
    collapsed to single spaces.
    """
    lines = [line for line in entry.splitlines()]
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    flattened = " ".join(" ".join(lines).split())
    if not flattened:
        return ""
    return f"- [{region}] {flattened}  _(from: {_QUEUE_SOURCE})_"


def strip_region_blocks_text(text: str) -> str:
    """The guide with both fenced region blocks removed, markers included."""
    from ciao.memory_tool import strip_region_blocks  # noqa: PLC0415

    return strip_region_blocks(text)


def read_region_text(text: str, region: str) -> tuple[list[str], list[Any]]:
    """Parse one region's entries out of guide TEXT rather than a file.

    ``memory_tool.read_region`` takes a path, and the split works on text it has
    already read, so this reuses the same marker regexes instead of a second
    parser. One definition of where a region starts and ends is the whole point.
    """
    import re  # noqa: PLC0415

    from ciao.memory_tool import _REGION_META, parse_entries, resolve_region  # noqa: PLC0415

    canonical = resolve_region(region)
    meta = _REGION_META[canonical]
    match = re.search(
        f"{meta['start_re']}(.*?){meta['end_re']}", text, re.DOTALL
    )
    if match is None:
        return [], [f"missing markers for {canonical}"]
    return parse_entries(match.group(1)), []


# -- P10.6: rebuild the derived artefacts per root ---------------------------


def rebuild_indexes(install_root: Path, workspaces: list[str]) -> dict[str, Any]:
    """Rebuild each root's INDEX.md and VOCABULARY.md, with no path prefix.

    Every index written before the migration describes the old layout: entries
    keyed under ``personal/...`` inside one shared vault. After the move each
    root holds exactly one vault, so a prefix has nothing to disambiguate and its
    presence would be a lie about where a note lives. A stale index is worse than
    an absent one, because it reads as current, which is why the pre-migration
    copies are stashed rather than left in place.

    Reports per root rather than aggregating: a failure in one root's rebuild
    must be attributable to that root, not hidden in a total.
    """
    from ciao.vault_index import format_vocabulary, scan_vault, write_index_file

    out: dict[str, Any] = {"rebuilt": [], "errors": []}
    for name in workspaces:
        vault = Path(install_root) / name / "memory-vault"
        if not vault.is_dir():
            out["errors"].append({"workspace": name, "error": f"no vault at {vault}"})
            continue
        try:
            entries = scan_vault(vault)
            write_index_file(entries, vault / "INDEX.md")
            (vault / "VOCABULARY.md").write_text(
                format_vocabulary(entries), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 — reported per root, never fatal
            out["errors"].append({"workspace": name, "error": str(exc)})
            continue
        out["rebuilt"].append({"workspace": name, "entries": len(entries)})
    return out


def rebuild_search_index(
    install_root: Path,
    workspaces: list[str],
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Drop and rebuild the full-text index against the new paths.

    Every row in the old database points at a path under the shared vault, so
    incremental repair is not possible: the rows are not stale, they are wrong.
    Dropping is the only honest option.

    ``db_path`` exists because ``fts_search.get_db_path`` resolves from the
    ambient ``CIAO_MEMORY_DIR`` (defaulting to ``~/.ciao``) and there is no
    per-install search database to derive from an install root. So migrating an
    install that is NOT the ambient one would otherwise drop the ambient
    install's index — the same environment leak P2 found in the os-audit command.
    A caller that knows which database belongs to the install it is migrating
    must say so; the default is the ambient one, which is correct for the normal
    case of migrating the install you are running in.
    """
    import sqlite3

    from ciao.fts_search import get_db_path, index_vault, init_db

    install_root = Path(install_root).resolve()
    db = Path(db_path) if db_path is not None else get_db_path()
    result: dict[str, Any] = {"database": str(db), "indexed": [], "errors": []}
    try:
        if db.exists():
            db.unlink()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
    except OSError as exc:
        result["errors"].append({"workspace": "", "error": f"could not reset {db}: {exc}"})
        return result
    try:
        init_db(conn)
        for name in workspaces:
            vault = Path(install_root) / name / "memory-vault"
            if not vault.is_dir():
                continue
            try:
                # Keyed against the install root, so `personal/memory-vault/...`
                # and `work/memory-vault/...` stay distinct. Keyed per root, both
                # collapsed to `memory-vault/...`: the second pass overwrote the
                # first root's rows and its prune deleted the rest, so a rebuild
                # left only the LAST root searchable while reporting both.
                indexed, removed = index_vault(conn, vault, path_base=install_root)
                result["indexed"].append(
                    {"workspace": name, "indexed": indexed, "removed": removed}
                )
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"workspace": name, "error": str(exc)})
    finally:
        conn.close()
    return result


# -- P10.5: triage the skill catalog, never copy it --------------------------

# The reference catalog is 20 skills of which 16 are work-scoped. Copying it into
# every root would rebuild the global catalog this release exists to end, and the
# migration cannot decide which skill belongs where: that is a judgement about
# the user's own work, exactly like the CLAUDE.md regions in P10.4. So the whole
# catalog moves to the primary root, where it keeps behaving as it does today,
# and a triage sheet lists every skill with its destination blank.
#
# Nothing packaged needs migrating: `sync_workspace_skills` reinstalls stock
# skills into each root's `.claude/skills` from package resources on every sync.
_SKILL_TRIAGE_RELATIVE = "Workspace/Skill-Triage.md"
_SKILLS_SRC = "skills-src"

# `skills-src/` is created for the user to promote the genuinely general few
# into as they triage. The design record says "create it empty"; it gets a README
# instead, because git cannot track an empty directory and this migration's undo
# is git-based, so an empty directory would be invisible to every check that
# proves the migration exact.
_SKILLS_SRC_README = """\
# skills-src

Shared skill sources, mirrored into every agent root. **Not a workspace**: it has
no vault, no guide and no sessions of its own.

Put a skill here only when it is genuinely general — it applies in every
workspace. Anything tied to one workspace's tools, accounts or vocabulary belongs
in that root's own `skills/` directory instead, which is where the whole
pre-migration catalog now lives.

See `Workspace/Skill-Triage.md` in the primary root's vault for the catalog that
was moved and still needs sorting.
"""


@dataclass(frozen=True, slots=True)
class SkillTriageEntry:
    """One skill awaiting a destination decision."""

    name: str
    origin: str
    description: str
    note: str = ""


@dataclass(slots=True)
class SkillsTriage:
    """What moves to the primary root, and what the user still has to decide."""

    primary: str
    moves: list[Move] = field(default_factory=list)
    entries: list[SkillTriageEntry] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary": self.primary,
            "moves": [
                {"source": m.source, "destination": m.destination, "workspace": m.workspace}
                for m in self.moves
            ],
            "entries": [
                {
                    "name": e.name,
                    "origin": e.origin,
                    "description": e.description,
                    "note": e.note,
                }
                for e in self.entries
            ],
            "refusals": list(self.refusals),
        }


def plan_skills_triage(install_root: Path, primary: str) -> SkillsTriage:
    """What the skill catalog does at migration time. Never writes.

    Two things move, and both go to the primary root:

    ``skills/`` is the custom catalog, mirrored into ``.claude/skills`` by
    ``sync_skills._rebuild_custom_skill_links`` from whichever root it sits in.

    ``skills-lock.json`` is the other half of the same non-stock catalog.
    ``_refresh_upstream_skills`` reads it from the root it is syncing, so leaving
    it at the install root would strand every upstream skill at a path no root
    ever reads. Moving it is also self-healing: the upstream copies live under
    the old ``.claude/skills`` and are not moved, so the primary's next sync sees
    them as missing and refetches them from the lock.
    """
    install_root = Path(install_root).resolve()
    triage = SkillsTriage(primary=primary)
    if not primary:
        triage.refusals.append(
            "no primary workspace, so there is no root to hold the skill catalog"
        )
        return triage

    for relative in ("skills", "skills-lock.json"):
        source = install_root / relative
        if not source.exists():
            continue
        destination = install_root / primary / relative
        if destination.exists():
            triage.refusals.append(
                f"destination for {relative} already exists: {destination}"
            )
            continue
        triage.moves.append(
            Move(
                source=relative,
                destination=f"{primary}/{relative}",
                workspace=primary,
            )
        )

    triage.entries = _skill_triage_entries(install_root)
    return triage


def _skill_triage_entries(install_root: Path) -> list[SkillTriageEntry]:
    """Every non-stock skill in the catalog, described well enough to triage.

    Descriptions come from ``skills_inventory.build_skill_inventory`` — the same
    enumerator the Settings page uses — rather than a second frontmatter parser.

    But the inventory keys on ``*/SKILL.md``, so a catalog DIRECTORY without one
    is invisible to it while still being moved by the migration. The reference
    install has exactly one such directory, whose only content is an ignored
    ``__pycache__``. Listing the directories from disk and taking descriptions
    from the inventory is what keeps this sheet a complete account of what moved,
    which is the same conservation rule the vault classification follows.
    """
    from ciao.skills_inventory import build_skill_inventory  # noqa: PLC0415

    described: dict[str, dict[str, Any]] = {}
    try:
        inventory = build_skill_inventory(install_root, include_content=False)
    except Exception:  # noqa: BLE001 — a broken inventory must not block the plan
        inventory = {"skills": []}
    for skill in inventory.get("skills", []):
        if not isinstance(skill, dict) or skill.get("label") == "stock":
            continue
        name = str(skill.get("name") or "")
        if name:
            described[name] = skill

    entries: list[SkillTriageEntry] = []
    listed: set[str] = set()
    catalog = install_root / "skills"
    if catalog.is_dir():
        for entry in sorted(catalog.iterdir(), key=lambda p: p.name):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            listed.add(entry.name)
            skill = described.get(entry.name, {})
            entries.append(
                SkillTriageEntry(
                    name=entry.name,
                    origin="skills/",
                    description=str(skill.get("description") or ""),
                    note=(
                        ""
                        if (entry / "SKILL.md").is_file()
                        else "no SKILL.md, so no root loads it"
                    ),
                )
            )

    for name, skill in sorted(described.items()):
        if name in listed:
            continue
        entries.append(
            SkillTriageEntry(
                name=name,
                origin=str(skill.get("source") or "skills-lock.json"),
                description=str(skill.get("description") or ""),
                note="",
            )
        )
    return entries


def format_skill_triage(triage: SkillsTriage, workspaces: list[str]) -> str:
    """The triage sheet: every skill listed, every destination blank.

    Deliberately not a proposal queue. A proposal is a claim the agent believes
    and asks to have confirmed; this sheet makes no claim at all, because a
    default destination is exactly the guess that would rebuild the global
    catalog. Blank means blank.
    """
    choices = " · ".join([*sorted(workspaces), _SKILLS_SRC, "delete"])
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    lines = [
        "---",
        "type: note",
        "title: Skill triage after re-rooting",
        (
            "description: Every skill from the pre-migration catalog, with its "
            "destination root left blank on purpose. Fill one in per row."
        ),
        "tags: [ciao, skills, migration, triage]",
        f"created: {stamp}",
        f"updated: {stamp}",
        "status: open",
        "---",
        "",
        "# Skill triage after re-rooting",
        "",
        (
            f"The whole catalog moved to `{triage.primary}/`, which is where it "
            "behaves exactly as it did before the migration. Nothing was copied "
            "into the other roots: with 16 of 20 skills scoped to one workspace, "
            "copying would have rebuilt the global catalog the re-rooting exists "
            "to end."
        ),
        "",
        "**Nothing here is a suggestion.** The destination column is blank because",
        "deciding which workspace a skill belongs to is a judgement about your own",
        "work, and a guess here misfiles a skill the same way the shared vault",
        "misfiled contacts.",
        "",
        "## How to move one",
        "",
        f"1. Write one of `{choices}` in the **Destination** column.",
        (
            "2. `git mv` the directory from "
            f"`{triage.primary}/skills/<name>` to `<destination>/skills/<name>`, "
            f"or to `{_SKILLS_SRC}/<name>` when it genuinely applies everywhere."
        ),
        (
            "3. Run a skill sync for both roots. The links under "
            "`.claude/skills` are rebuilt from `skills/`, so nothing else needs "
            "editing."
        ),
        (
            "4. Upstream rows (anything not sourced from `skills/`) are pinned in "
            "`skills-lock.json`, which moved with the catalog. Move the lock "
            "entry, not the directory: the copy under `.claude/skills` is "
            "refetched."
        ),
        "",
        "## Catalog",
        "",
        "| Skill | Source | Destination | What it does |",
        "| --- | --- | --- | --- |",
    ]
    for entry in triage.entries:
        description = _one_cell(entry.description) or "—"
        if entry.note:
            description = f"{description} _({entry.note})_"
        lines.append(f"| `{entry.name}` | {_one_cell(entry.origin)} |  | {description} |")
    lines.extend(
        [
            "",
            f"{len(triage.entries)} skill(s) to triage.",
            "",
        ]
    )
    return "\n".join(lines)


def _one_cell(text: str) -> str:
    """Collapse text to one markdown table cell.

    A newline ends the row and a bare pipe ends the cell, so a SKILL.md
    description containing either would silently shred the table. The
    descriptions in the reference catalog are multi-sentence and several are YAML
    block scalars, so this is the normal case rather than an edge one.
    """
    flattened = " ".join((text or "").split())
    return flattened.replace("|", "\\|")


def write_skills_triage(
    install_root: Path,
    triage: SkillsTriage,
    workspaces: list[str],
    primary_vault: str,
) -> list[str]:
    """Create ``skills-src/`` and the triage sheet. Returns what it created.

    The paths come back so the receipt can record them and ``undo`` can remove
    exactly what the migration added, rather than deleting by pattern.
    """
    created: list[str] = []
    install_root = Path(install_root)

    source_dir = install_root / _SKILLS_SRC
    source_dir.mkdir(parents=True, exist_ok=True)
    readme = source_dir / "README.md"
    if not readme.exists():
        readme.write_text(_SKILLS_SRC_README, encoding="utf-8")
        created.append(f"{_SKILLS_SRC}/README.md")

    # No catalog means no sheet. A triage document listing nothing is noise in a
    # vault, and a fresh install has nothing to sort.
    if triage.entries and primary_vault:
        doc = install_root / primary_vault / _SKILL_TRIAGE_RELATIVE
        if not doc.exists():
            doc.parent.mkdir(parents=True, exist_ok=True)
            doc.write_text(format_skill_triage(triage, workspaces), encoding="utf-8")
            created.append(f"{primary_vault}/{_SKILL_TRIAGE_RELATIVE}")
    return created
