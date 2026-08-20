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


def dirty_tracked_paths(install_root: Path, relative: str) -> list[str]:
    """Tracked files under ``relative`` with staged or unstaged modifications.

    Scoped to TRACKED changes on purpose (P10.2). The reference install carries
    roughly 700 untracked ``Logs/Chats/chat-*`` directories at any moment, so a
    strict whole-tree gate would refuse on every real install and nothing would
    ever migrate. What the gate protects is ``git checkout`` staying a working
    undo for content that is under version control; an untracked chat log has no
    committed state to lose.
    """
    code, out = _run_git(install_root, "status", "--porcelain", "--untracked-files=no", "--", relative)
    if code != 0 or not out:
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


_REGENERATED_BACKUP = "migration/reroot-regenerated"


def apply(
    install_root: Path,
    vault_root: Path,
    workspaces: list[str],
    runtime_root: Path,
) -> dict[str, Any]:
    """Re-root every registered workspace, or none of them.

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
    payload = result.as_dict()
    payload["recorded_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if result.refused:
        payload["status"] = "refused"
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload

    dirty = dirty_tracked_paths(install_root, vault_root.name)
    if dirty:
        payload["status"] = "refused"
        payload["refusals"] = [
            f"{len(dirty)} tracked file(s) under {vault_root.name}/ have uncommitted "
            "changes; commit or stash them so git checkout stays a working undo"
        ]
        payload["dirty_tracked"] = dirty[:20]
        payload["refused"] = True
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload

    code, head = _run_git(install_root, "rev-parse", "HEAD")
    payload["git_head_before"] = head if code == 0 else ""

    applied: list[dict[str, str]] = []
    for move in result.moves:
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

    payload["status"] = "migrated"
    payload["applied"] = applied
    payload["stashed_files"] = stashed
    payload["removed_vault_dir"] = removed_vault
    payload["receipt_path"] = str(write_receipt(runtime_root, payload))
    return payload


def undo(install_root: Path, runtime_root: Path) -> dict[str, Any]:
    """Reverse a completed re-rooting to a byte-identical tree.

    CLI only. Reverting the architecture is not a housekeeping button, and the
    only reason it exists is so the forward migration is provably exact rather
    than merely tested.
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

    remove_receipt(runtime_root)
    return {
        "status": "undone",
        "reversed": reversed_moves,
        "restored_stashed": restored,
    }


def remove_receipt(runtime_root: Path) -> bool:
    """Drop the receipt so a later run is not gated by a reverted migration."""
    path = receipt_path(runtime_root)
    if path.is_file():
        path.unlink()
        return True
    return False


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
