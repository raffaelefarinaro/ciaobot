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

import errno
import hashlib
import json
import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

RECEIPT_VERSION = 1

# The vault directory's leaf name when nothing says otherwise. It is
# configurable (`CIAO_VAULT_ROOT`), so anything deriving a per-root vault path
# must take it as an argument rather than assume it; `plan()` reads it off the
# real vault, and the rebuild helpers now accept it for the same reason.
VAULT_DIR_NAME = "memory-vault"

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


def mark_born_per_root(
    install_root: Path,
    runtime_root: Path,
    workspaces: list[str],
    *,
    origin: str = "born",
) -> list[Path]:
    """Record that this install was CREATED in the per-root layout.

    A fresh install has nothing to migrate, but ``agent_root`` answers per-root
    only when a receipt says the layout is per-root — the receipt is the layout
    discriminator, not migration bookkeeping. Without one, a brand-new install
    would have its files nested while every consumer still resolved the install
    root, which is the one combination that breaks everything downstream.

    ``status: "migrated"`` because that is what the gate reads, with no moves.
    ``origin`` distinguishes the two ways an install arrives here without the
    engine — ``"born"`` for a fresh setup, ``"hand"`` for a vault moved by hand or
    by a model — so a later reader can tell them apart, and tell both from a real
    migration, instead of inferring it from an empty move list.
    """
    payload = {
        "schema_version": RECEIPT_VERSION,
        "status": "migrated",
        "born_per_root": origin == "born",
        "origin": origin,
        "install_root": str(Path(install_root).resolve()),
        "vault_root": str(Path(install_root).resolve() / VAULT_DIR_NAME),
        "workspaces": sorted(workspaces),
        "moves": [],
        "applied": [],
        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    from ciao.config import reset_reroot_cache

    path = write_receipt(runtime_root, payload)
    reset_reroot_cache()
    return [path]


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


def peek_receipt(runtime_root: Path) -> dict[str, Any] | None:
    """The receipt file whatever its status, for the detection side.

    ``read_receipt`` gates on ``status == "migrated"`` and must, because a
    refused or surveyed receipt is work still to do. But a detector whose whole
    job is to surface that work needs to READ the refusal, so it reads this
    instead — the same split ``vault_rehome`` already makes for the same reason.
    """
    path = receipt_path(runtime_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def write_receipt(runtime_root: Path, payload: dict[str, Any]) -> Path:
    """Persist a receipt atomically, keeping any earlier one beside it.

    A COMPLETED receipt is never downgraded. This file is the SOLE layout
    discriminator — ``config._rerooted``, ``agent_roots_for`` and ``logs_root``
    all ask ``read_receipt``, which answers only on ``status == "migrated"`` — so
    replacing a migrated receipt with a survey or a refusal does not merely lose
    the reverse map, it moves every agent root back to the install root: no
    guide, no skills, and ``undo`` answering ``nothing_to_undo`` because there is
    no migrated receipt left to reverse. A single
    ``ciao workspace-reroot --rehearse`` on a migrated install did exactly that.
    ``rehearse`` is documented as writing nothing that can make the real
    migration look done; the inverse holds too. So a non-migrated payload is
    dropped when the migration is already recorded — the same guard
    ``vault_rehome.write_receipt`` makes, for the same reason.
    """
    path = receipt_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload.get("status") != "migrated":
        existing = peek_receipt(runtime_root)
        if existing is not None and existing.get("status") == "migrated":
            return path  # a survey or refusal must not un-migrate the install
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

    # Before anything else, because everything else assumes it: the moves are
    # `git mv` and the undo is `git checkout`. An install with no repository used
    # to fail on the first move and refuse forever; it now gets one, with a
    # snapshot commit as the rollback point.
    history = ensure_rollback_history(install_root)

    result = plan(install_root, vault_root, workspaces)
    triage = plan_skills_triage(install_root, primary)
    guides = guide_moves(install_root, primary)
    payload = result.as_dict()
    payload["primary"] = primary
    payload["skills_triage"] = triage.as_dict()
    payload["recorded_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    payload["git_history"] = history

    history_refusal: list[str] = []
    if history["status"] in {"no_git_binary", "init_failed", "add_failed", "commit_failed"}:
        detail = history.get("error") or history["status"]
        history_refusal = [
            "the install has no git history to roll back to and one could not be "
            f"created ({detail}); every move is a git mv and git checkout is the "
            "undo, so migrating without it would be unrecoverable"
        ]

    if result.refused or triage.refusals or history_refusal:
        payload["status"] = "refused"
        payload["refused"] = True
        # `unclassified` is a refusal reason too — the plan refuses on it — but it
        # was not part of `refusals`, so a run blocked solely by an unrecognised
        # vault directory reported `status: refused` with an EMPTY reason list.
        # That is what the blocking gate renders, so the operator was told to fix
        # something and not told what.
        unclassified = [
            f"{item} is in the vault but the migration has no destination for it; "
            "register it as a workspace or move it out of the vault"
            for item in result.unclassified
        ]
        payload["refusals"] = [
            *history_refusal,
            *result.refusals,
            *triage.refusals,
            *unclassified,
        ]
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

    moved_relatives = [
        vault_root.name,
        *(m.source for m in triage.moves),
        *(m.source for m in guides),
    ]
    dirty = dirty_tracked_paths(install_root, *moved_relatives)
    if dirty:
        payload["status"] = "refused"
        # Name the dirty FILE, not the list of paths being watched. Listing every
        # watched root put six directory names in front of the one fact the
        # operator needs, in the message the blocking gate shows them.
        first = dirty[0]
        others = f" (and {len(dirty) - 1} more)" if len(dirty) > 1 else ""
        payload["refusals"] = [
            f"{first}{others} has uncommitted changes; commit or stash it so "
            "git checkout stays a working undo"
        ]
        payload["dirty_tracked"] = dirty[:20]
        payload["refused"] = True
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload

    code, head = _run_git(install_root, "rev-parse", "HEAD")
    payload["git_head_before"] = head if code == 0 else ""

    # Read the shared guide before it moves. The split is computed here so a
    # failure to parse it refuses the run rather than leaving roots half-guided.
    shared_guide = install_root / "CLAUDE.md"
    split: GuideSplit | None = None
    if shared_guide.is_file():
        try:
            split = split_guide(
                shared_guide.read_text(encoding="utf-8"), result.workspaces, primary
            )
        except Exception as exc:  # noqa: BLE001
            payload["status"] = "refused"
            payload["refused"] = True
            payload["refusals"] = [f"could not split {shared_guide}: {exc}"]
            payload["receipt_path"] = str(write_receipt(runtime_root, payload))
            return payload

    applied: list[dict[str, str]] = []
    # The skill catalog moves through the same loop as the vaults, so one failure
    # rolls back the whole run and one receipt reverses it. A catalog left behind
    # by a successful vault migration would be a half-rooted install by another
    # name: the primary root would load no custom skill at all.
    pruned_empty: list[str] = []
    for move in [*result.moves, *triage.moves, *guides]:
        source = install_root / move.source
        destination = install_root / move.destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not any(source.iterdir()):
            # `git mv` fails on an empty directory ("source directory is empty")
            # and one failure refuses the whole run — so a vault holding an empty
            # `Templates/`, which is an ordinary thing for someone who never used
            # templates, could not migrate at all. Found by booting a released
            # bundle against a synthetic pre-migration install, which is the path
            # a real upgrade takes.
            #
            # There is nothing to move: git cannot track an empty directory, so it
            # holds no content and no history. It is removed instead, which also
            # lets the vault directory itself be pruned afterwards, and recorded
            # so the receipt still accounts for every path.
            try:
                source.rmdir()
            except OSError:
                pass
            pruned_empty.append(move.source)
            continue
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

    # Everything past this point mutates the tree OUTSIDE git's reach - stashing
    # regenerated notes, pruning the vault directory, rewriting the registry,
    # writing the receipt - and nothing is committed until it all succeeds. An
    # exception here used to leave the install half-rooted with no receipt at
    # all: startup caught it and carried on with a config still naming paths
    # that had already moved, and undo had nothing to work from. So the same
    # rollback the git mv loop does covers this stretch too, and the refusal is
    # always recorded. The receipt is inside the stretch on purpose: it is what
    # flips agent_root onto the layout the moves just built, so a run whose
    # write fails has to unwind like any other instead of leaving files migrated
    # under a config still answering the shared layout.
    backup_dir = runtime_root / _REGENERATED_BACKUP
    stashed: list[dict[str, Any]] = []
    removed_vault = False
    # The before-image the registry rewrite captures, hoisted above the try so
    # the unwind can restore it; if the rewrite itself is what failed, the file
    # was never touched (it writes atomically) and None is exactly right.
    registry_before: list[dict[str, Any]] | None = None
    # Hoisted beside it: what this run CREATED, which the unwind removes too -
    # leftovers inside a destination root would leave it non-empty and make
    # every retry refuse forever.
    created: list[str] = []
    created_dirs: list[str] = []
    try:
        # Both the generated notes and the ignorable cruft are STASHED, not deleted.
        # Undo has to restore a byte-identical tree with no caveats, and recreating a
        # sidecar file empty is not identical. Three notes and a Finder sidecar cost
        # nothing to keep, and an exactly-undoable migration is the whole argument for
        # rewriting a user's layout at all.
        backup_dir.mkdir(parents=True, exist_ok=True)
        for relative in [*result.regenerated, *result.ignored]:
            source = install_root / relative
            if not source.is_file():
                continue
            # Two of these are TRACKED on the reference install (the vault root's
            # MEMORY.md and VOCABULARY.md), so moving them out of the worktree
            # without telling git leaves the index claiming files that are gone.
            # Stage the removal so the migration's git state is self-consistent and
            # whoever commits it does not carry two ghost entries.
            tracked = (
                _run_git(install_root, "ls-files", "--error-unmatch", "--", relative)[0] == 0
            )
            target = backup_dir / Path(relative).name
            # `Path.replace` is os.rename, which fails with EXDEV when the
            # runtime root sits on another filesystem - and CIAO_RUNTIME_ROOT
            # can put it anywhere. shutil.move falls back to copy+unlink.
            shutil.move(str(source), str(target))
            if tracked:
                _run_git(install_root, "rm", "--cached", "--quiet", "--", relative)
            stashed.append(
                {
                    "source": relative,
                    "backup": str(target.relative_to(runtime_root)),
                    "tracked": tracked,
                }
            )

        # The vault directory is empty now. Remove it so the layout has exactly one
        # home per workspace, and record that undo recreates it.
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

        vault_destinations = {m.workspace: m.destination for m in result.moves if m.workspace}
        primary_vault = vault_destinations.get(primary, "")
        created.extend(
            write_skills_triage(install_root, triage, result.workspaces, primary_vault)
        )

        if split is not None:
            guide_created, guide_stashed = write_guide_split(
                install_root, split, vault_destinations, backup_dir, runtime_root
            )
            created.extend(guide_created)
            stashed.extend(guide_stashed)
            payload["guide_split"] = {
                "primary": split.primary,
                "queued": {name: len(items) for name, items in split.queued.items()},
            }

        # Every root gets its agent assets, through the same code --repair uses, so
        # "after a migration, --repair is a no-op" is an invariant and not a hope.
        shared_sources = install_root / _SKILLS_SRC
        for name in result.workspaces:
            files, dirs = bootstrap_root(install_root / name, shared_sources)
            created.extend(f"{name}/{relative}" for relative in files)
            created_dirs.extend(f"{name}/{relative}" for relative in dirs)
        payload["created_files"] = created
        payload["created_dirs"] = created_dirs
        # What the bootstrap put inside each directory it created, recorded so
        # undo can tell the migration's own contents from anything an operator
        # did underneath afterwards. The directory name alone made undo delete
        # a user's later files along with the bootstrap's, and names alone still
        # could not see an edit made IN PLACE to a seeded command — which the
        # migration invites — so each file is hashed, each symlink has its
        # target recorded, and undo refuses unless everything matches.
        payload["created_dirs_contents"] = _snapshot_created_dirs(
            install_root, created_dirs
        )

        # Sessions are keyed by cwd, and the cwd just changed. Flag rather than
        # pretend, and report the count so a user whose long chat resets knows why.
        sessions = flag_stranded_sessions(runtime_root)
        payload["stranded_sessions"] = sessions

        # The receipt is the LAST step inside the transaction: committing the
        # layout flip and holding every other mutation to the same rollback is
        # one decision, not two. A failure here unwinds the run below rather
        # than returning success with no receipt committed.
        payload["status"] = "migrated"
        payload["applied"] = applied
        payload["pruned_empty"] = pruned_empty
        payload["stashed_files"] = stashed
        payload["removed_vault_dir"] = removed_vault
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))

    except Exception as exc:  # noqa: BLE001 - any failure has to unwind
        for entry in reversed(stashed):
            restored = install_root / entry["source"]
            restored.parent.mkdir(parents=True, exist_ok=True)
            backup = runtime_root / entry["backup"]
            if backup.is_file():
                shutil.move(str(backup), str(restored))
            # `.get`, not `[`: the guide split stashes queue backups without a
            # tracked key, and one of those ahead of the failure used to kill
            # the unwind itself with a KeyError.
            if entry.get("tracked"):
                _run_git(install_root, "add", "--", entry["source"])
        if removed_vault:
            vault_root.mkdir(parents=True, exist_ok=True)
        for done in reversed(applied):
            _run_git(install_root, "mv", done["destination"], done["source"])
        # What the run itself wrote comes back out, or the leftovers keep every
        # destination root non-empty and the next attempt refuses forever. Only
        # recorded paths, never a pattern.
        for relative in reversed(created):
            target = install_root / relative
            if target.is_symlink() or target.is_file():
                target.unlink()
            _prune_empty_parents(install_root, target.parent)
        for relative in reversed(created_dirs):
            target = install_root / relative
            if target.is_dir():
                shutil.rmtree(target)
            _prune_empty_parents(install_root, target.parent)
        # The registry moved with the files, so it moves BACK with them.
        # Leaving the rewritten entries pointing at per-root paths while the
        # vaults sit back under the shared root is how a failed unattended run
        # makes every workspace look empty.
        refusals = [f"migration failed after moving files: {exc}"]
        if registry_before is not None:
            try:
                _write_registry(runtime_root, registry_before)
            except Exception as restore_exc:  # noqa: BLE001 - surfaced, never swallowed
                refusals.append(
                    "the workspace registry could not be restored either "
                    f"({restore_exc}); its entries still name per-root paths "
                    "while the vaults are back under the shared root, so repair "
                    "it by hand before starting the app"
                )
        # A failed receipt write can leave half a file at the path; nothing
        # short of a completed migration may sit there gating the next start.
        stray = receipt_path(runtime_root)
        stray.unlink(missing_ok=True)
        stray.with_suffix(".json.tmp").unlink(missing_ok=True)
        payload["status"] = "refused"
        payload["refused"] = True
        payload["refusals"] = refusals
        payload["receipt_path"] = str(write_receipt(runtime_root, payload))
        return payload

    # The receipt is what flips CiaoConfig.agent_root, so the cached answer from
    # before the migration is now stale in this process.
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    return payload


def _replace_across_filesystems(source: Path, destination: Path) -> None:
    """Replace ``destination`` with ``source``, even across filesystems.

    why: ``Path.replace`` is rename(2), which fails with EXDEV when the runtime
    root (``CIAO_RUNTIME_ROOT``) sits on a different filesystem from the install
    root. The forward migration stashes INTO the runtime root with
    ``shutil.move`` precisely because that layout is supported, so this reverse
    step needs the same reach. The fast path stays an atomic rename; on EXDEV
    the fallback copies beside the DESTINATION and ``os.replace``s onto it, so
    the overwrite stays atomic and a failed copy leaves the destination
    untouched. Errors propagate: a failed restore still aborts the undo.
    """
    try:
        source.replace(destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    tmp = destination.with_name(destination.name + ".ciao-restore.tmp")
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, destination)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


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

    Refuses without touching anything when a bootstrap-created directory does
    not hold exactly what the migration left in it: a file an operator added
    underneath, a seeded command edited in place, or a recorded file deleted.
    An architecture rollback must not be the thing that destroys any of those,
    and the directory is removed whole, so the guard compares names AND content
    fingerprints before a single path is touched.
    """
    install_root = Path(install_root).resolve()
    runtime_root = Path(runtime_root)
    receipt = read_receipt(runtime_root)
    if receipt is None:
        return {"status": "nothing_to_undo", "reason": "no migrated receipt"}

    # Before ANY mutation: a directory the migration created may have gained
    # descendants since, or had its seeded contents edited or removed, and
    # everything below assumes the directories hold exactly what the receipt
    # recorded.
    blocked = _unexpected_under_created_dirs(install_root, receipt)
    if blocked:
        shown = ", ".join(blocked[:5])
        more = f" (and {len(blocked) - 5} more)" if len(blocked) > 5 else ""
        return {
            "status": "refused",
            "reason": (
                "these paths under directories the migration created were "
                f"added, edited or deleted after it ran: {shown}{more}. Restore "
                "each one to what the migration left or move it out, then run "
                "the undo again; undoing anyway would delete them with their "
                "directory and git cannot bring them back"
            ),
            "unexpected_paths": blocked,
        }

    vault_name = Path(receipt.get("vault_root", "")).name or VAULT_DIR_NAME
    if receipt.get("removed_vault_dir"):
        (install_root / vault_name).mkdir(parents=True, exist_ok=True)

    restored: list[str] = []
    for entry in receipt.get("stashed_files", []):
        backup = runtime_root / entry["backup"]
        target = install_root / entry["source"]
        if backup.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace_across_filesystems(backup, target)
            # Re-stage only what WAS tracked. `git add` on a file that was
            # untracked before would newly track it, which is not a restoration.
            if entry.get("tracked"):
                _run_git(install_root, "add", "--", entry["source"])
            restored.append(entry["source"])

    # Files the migration CREATED are removed before the moves are reversed,
    # because they live inside the directories about to move back. Removed by
    # recorded path rather than by pattern, so a file the user wrote into the
    # same folder afterwards is never touched.
    removed: list[str] = []
    for relative in receipt.get("created_files", []):
        target = install_root / relative
        # is_symlink first: AGENTS.md is a symlink, and `is_file()` follows it,
        # so a link whose target already moved back would be left dangling.
        if target.is_symlink() or target.is_file():
            target.unlink()
            # If the migration was COMMITTED, these paths are tracked. Deleting
            # the file without staging it leaves the index claiming a file that
            # is gone, and `git mv` of any ancestor directory then fails with
            # "bad source" — which is exactly how an undo stalled halfway on the
            # reference install.
            _run_git(install_root, "rm", "--cached", "--quiet", "--", relative)
            removed.append(relative)
            _prune_empty_parents(install_root, target.parent)
    source_dir = install_root / _SKILLS_SRC
    if source_dir.is_dir() and not any(source_dir.iterdir()):
        source_dir.rmdir()

    # The bootstrap's `.claude/` holds hundreds of packaged files, so it is
    # recorded as a directory and removed whole rather than listed file by file.
    # Only directories this migration CREATED are listed, so a root that already
    # had one keeps it. Removing whole is safe because the guard above verified
    # every descendant against the name-and-fingerprint capture the receipt
    # recorded; anything added, edited or deleted since refused the undo before
    # this line ran.
    import shutil  # noqa: PLC0415

    for relative in receipt.get("created_dirs", []):
        target = install_root / relative
        if target.is_dir():
            shutil.rmtree(target)
            # Same reason as the created files: once the migration is committed
            # everything under here is tracked, so the index has to follow the
            # worktree or `git mv` of an ancestor fails.
            _run_git(install_root, "rm", "-r", "--cached", "--quiet", "--", relative)
            removed.append(relative)
            _prune_empty_parents(install_root, target.parent)

    reversed_moves: list[str] = []
    already: list[str] = []
    for entry in reversed(receipt.get("applied", [])):
        source = install_root / entry["source"]
        destination = install_root / entry["destination"]
        # Resumable on purpose. A first attempt can stop partway — the reference
        # install hit exactly that, because a stale guide recreated at the old
        # path blocked one reversal — and re-running then failed on the moves it
        # had ALREADY reversed, leaving no way forward but hand-editing. A move
        # whose source is back and whose destination is gone is done, not broken.
        if not destination.exists() and not destination.is_symlink() and (
            source.exists() or source.is_symlink()
        ):
            already.append(entry["source"])
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", entry["destination"], entry["source"])
        if code != 0:
            return {
                "status": "failed",
                "reason": f"git mv failed reversing {entry['destination']}: {out}",
                "reversed": reversed_moves,
                "already_reversed": already,
                "remaining": [
                    e["destination"]
                    for e in reversed(receipt.get("applied", []))
                    if e["source"] not in reversed_moves and e["source"] not in already
                ],
            }
        reversed_moves.append(entry["source"])
        # Drop the now-empty root directory the migration created.
        parent = (install_root / entry["destination"]).parent
        if parent != install_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    before = receipt.get("registry_before")
    if before is not None:
        _write_registry(runtime_root, before)

    cleared = clear_stranded_sessions(
        runtime_root, list((receipt.get("stranded_sessions") or {}).get("flagged") or [])
    )

    remove_receipt(runtime_root)
    from ciao.config import reset_reroot_cache

    reset_reroot_cache()
    return {
        "status": "undone",
        "reversed": reversed_moves,
        "restored_stashed": restored,
        "removed_created": removed,
        "cleared_handover_flags": cleared,
        "already_reversed": already,
    }


def _walk_entries(base: Path) -> set[str]:
    """Every file, directory and symlink under ``base``, relative to it.

    ``os.walk`` with ``followlinks=False`` rather than ``rglob``: a symlinked
    directory is recorded once as an entry and never descended into, so the
    mirror farms inside `.claude/` are named without pulling in their targets'
    trees. The same walk at apply time and at undo time is what makes the
    comparison a diff of like against like.
    """
    entries: set[str] = set()
    for current, dirs, files in os.walk(base, followlinks=False):
        prefix = Path(current).relative_to(base)
        for name in [*dirs, *files]:
            entries.add(str(prefix / name))
    return entries


def _entry_fingerprint(path: Path) -> dict[str, Any]:
    """One descendant as the receipt remembers it.

    A regular file is HASHED, not named: the migration seeds commands an
    operator is meant to edit, so an in-place modification is exactly the case
    a name-only capture misses — the names still match the receipt while undo
    rmtree's the directory and destroys the edit with it. A symlink records its
    target STRING instead of hashing through it, which would store the target's
    content and miss a retarget. A directory stays name-based; its descendants
    carry their own entries.
    """
    if path.is_symlink():
        return {"target": os.readlink(path)}
    if path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 16), b""):
                digest.update(chunk)
        return {"size": path.stat().st_size, "sha256": digest.hexdigest()}
    return {}


def _fingerprint_tree(base: Path) -> dict[str, dict[str, Any]]:
    """Every descendant of ``base`` fingerprinted, keyed relative to it.

    The same walk ``_walk_entries`` does — ``followlinks=False``, so a symlinked
    directory is one entry and never descended into — carrying each entry's
    fingerprint, so apply time and undo time diff like against like.
    """
    records: dict[str, dict[str, Any]] = {}
    for current, dirs, files in os.walk(base, followlinks=False):
        prefix = Path(current).relative_to(base)
        for name in [*dirs, *files]:
            records[str(prefix / name)] = _entry_fingerprint(Path(current) / name)
    return records


def _snapshot_created_dirs(
    install_root: Path, created_dirs: list[str]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Fingerprint what the bootstrap put inside each directory it created.

    Recorded in the receipt beside the directory names: each file's sha256 and
    size, each symlink's target string, each directory by name alone. Names
    alone made undo rmtree the whole directory — and with it anything an
    operator added or edited beneath it after the migration ran.
    """
    contents: dict[str, dict[str, dict[str, Any]]] = {}
    for relative in created_dirs:
        base = Path(install_root) / relative
        if base.is_dir():
            contents[relative] = _fingerprint_tree(base)
    return contents


def _entry_matches_capture(path: Path, recorded: dict[str, Any]) -> bool:
    """Whether one live descendant is still exactly what the receipt recorded.

    Recorded-but-gone counts as changed: the receipt says the bootstrap wrote
    it, so its absence is a deletion undo must not paper over. An unreadable
    file counts as changed too — unknown state refuses rather than guessing.
    """
    if not (path.is_symlink() or path.exists()):
        return False
    try:
        return _entry_fingerprint(path) == recorded
    except OSError:
        return False


def _unexpected_under_created_dirs(
    install_root: Path, receipt: dict[str, Any]
) -> list[str]:
    """Descendants of bootstrap-created directories the migration did not leave.

    Each recorded directory's live tree is diffed against the fingerprint its
    receipt entry captured: an entry that appeared, a file whose bytes no longer
    hash to what the bootstrap wrote, a symlink retargeted, or a recorded file
    that has vanished all block the undo, named by path, because removing the
    directory whole would destroy them and the git moves that follow cannot
    bring them back. A receipt written before the captures existed — or by the
    build that recorded names alone — carries nothing verifiable per
    descendant, so every live entry counts as unattributed and none of it may
    be deleted on a guess.
    """
    unexpected: list[str] = []
    contents = receipt.get("created_dirs_contents")
    for relative in receipt.get("created_dirs", []):
        base = Path(install_root) / str(relative)
        if not base.is_dir():
            continue
        recorded = (contents or {}).get(str(relative))
        if not isinstance(recorded, dict):
            # No capture at all, or the earlier names-only shape: no digest to
            # hold a live file against, so nothing beneath this directory is
            # attributable.
            for entry in sorted(_walk_entries(base)):
                unexpected.append(f"{relative}/{entry}")
            continue
        for entry in sorted({*recorded, *_walk_entries(base)}):
            want = recorded.get(entry)
            if want is None or not _entry_matches_capture(base / entry, want):
                unexpected.append(f"{relative}/{entry}")
    return sorted(unexpected)


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
    """What each root's ``CLAUDE.md`` gets, and what gets queued for review.

    ``queued`` is the rendered bullet per root, kept because the one-bullet-per-
    line invariant is asserted against it. ``queued_proposals`` is the same thing
    typed, which is what actually reaches the queue: rendering here and parsing
    it back at the write site would be a second definition of the bullet format,
    and three copies of one bullet regex drifting apart is what P1 existed to fix.
    """

    primary: str
    per_root: dict[str, str]
    queued: dict[str, list[str]]
    queued_proposals: dict[str, list[Any]] = field(default_factory=dict)


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
    from ciao.memory_tool import REGIONS, _REGION_META  # noqa: PLC0415

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
    queued_proposals: dict[str, list[Any]] = {}
    for name in workspaces:
        if name == primary:
            per_root[name] = guide_text
            continue
        per_root[name] = secondary_text
        proposals: list[Any] = []
        for region in REGIONS:
            for entry in region_entries[region]:
                proposal = _queue_proposal(region, entry)
                if proposal is not None:
                    proposals.append(proposal)
        queued_proposals[name] = proposals
        queued[name] = [proposal.as_bullet() for proposal in proposals]
    return GuideSplit(
        primary=primary,
        per_root=per_root,
        queued=queued,
        queued_proposals=queued_proposals,
    )


_QUEUE_SOURCE = "shared CLAUDE.md before re-rooting"


def _queue_proposal(region: str, entry: str) -> Any:
    """One region entry as a queue proposal, or None if it is scaffolding.

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
    from ciao.memory_proposals import MemoryProposal  # noqa: PLC0415

    lines = [line for line in entry.splitlines()]
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("#")):
        lines.pop(0)
    flattened = " ".join(" ".join(lines).split())
    if not flattened:
        return None
    return MemoryProposal(target=region, text=flattened, source_section=_QUEUE_SOURCE)


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


# -- P10.6, session half: say so rather than faking continuity ---------------


def chat_store_file(runtime_root: Path) -> Path:
    return Path(runtime_root) / "web_projects.json"


def flag_stranded_sessions(runtime_root: Path) -> dict[str, Any]:
    """Mark every open chat's provider session as needing a context handover.

    Every live chat's provider session is keyed by the cwd it was started in, and
    the migration changes that cwd, so the session cannot be found again. The
    honest response is to carry a context capsule into a fresh session rather
    than let the next turn silently forget: ``handover_context_pending`` is
    exactly the flag the fork and provider-switch paths already use for this.

    "Handover" here is provider-SESSION context carry-over, not the multi-device
    host/client role handover in ``ciao/node_state.py`` — the flag predates the
    role rename and is persisted, so only the wording can be clarified, not the
    key renamed.

    Considered and rejected: symlinking the old ``~/.claude/projects/<slug>`` to
    the new one. It is an undocumented SDK layout outside the workspace, it would
    break session listing for both slugs, and one old slug maps to N new ones.

    Rewrites the raw dict rather than round-tripping through ``ChatInfo``, so an
    unknown key a future release adds survives a migration meant to set one
    field. Records only the ids it changed, not the whole 2.6 MB store, so undo
    clears exactly those flags and the receipt stays readable.

    NOTE: this writes the state file directly. The migration moves the vault out
    from under a running server anyway, so it must be run with the app stopped;
    a live server would otherwise overwrite this from its in-memory copy on its
    next save.
    """
    path = chat_store_file(runtime_root)
    if not path.is_file():
        return {"flagged": [], "reason": "no chat store"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        return {"flagged": [], "reason": f"could not read {path}: {exc}"}
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, dict):
        return {"flagged": [], "reason": "chat store has no chats map"}

    flagged: list[str] = []
    for chat_id, chat in chats.items():
        if not isinstance(chat, dict):
            continue
        # An archived chat has no live session to strand, and one that never
        # started a provider session has nothing to hand over.
        if chat.get("archived") or not str(chat.get("session_id") or "").strip():
            continue
        if chat.get("handover_context_pending"):
            continue  # already pending for another reason; leave it to its owner
        chat["handover_context_pending"] = True
        flagged.append(str(chat_id))

    if flagged:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    return {"flagged": flagged}


def clear_stranded_sessions(runtime_root: Path, chat_ids: list[str]) -> int:
    """Undo half of :func:`flag_stranded_sessions`, by recorded id only."""
    path = chat_store_file(runtime_root)
    if not path.is_file() or not chat_ids:
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return 0
    chats = data.get("chats") if isinstance(data, dict) else None
    if not isinstance(chats, dict):
        return 0
    cleared = 0
    for chat_id in chat_ids:
        chat = chats.get(str(chat_id))
        if isinstance(chat, dict) and chat.get("handover_context_pending"):
            chat["handover_context_pending"] = False
            cleared += 1
    if cleared:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    return cleared


# -- P10.4 applied: give every root its own guide ----------------------------

# The shared guide and its native alias both move to the primary root, so history
# follows and the primary's regions are byte-identical to what its sessions read
# today. They must not be left behind: providers walk UP from cwd for a guide, so
# a surviving `<install>/CLAUDE.md` would re-inject the primary's memory regions
# into every root and undo the split it just performed.
_GUIDE_NAMES = ("CLAUDE.md", "AGENTS.md")

_QUEUE_RELATIVE = "Workspace/Memory-Proposals.md"


def guide_moves(install_root: Path, primary: str) -> list[Move]:
    """Moves that carry the shared guide into the primary root."""
    install_root = Path(install_root)
    moves: list[Move] = []
    if not primary:
        return moves
    for name in _GUIDE_NAMES:
        source = install_root / name
        # is_symlink first: AGENTS.md is a relative symlink to CLAUDE.md, and
        # `exists()` on a symlink follows it, so a broken one would be skipped
        # and then dangle after its target moved.
        if not (source.is_symlink() or source.exists()):
            continue
        if (install_root / primary / name).exists():
            continue
        moves.append(
            Move(source=name, destination=f"{primary}/{name}", workspace=primary)
        )
    return moves


def write_guide_split(
    install_root: Path,
    split: GuideSplit,
    vault_destinations: dict[str, str],
    backup_dir: Path,
    runtime_root: Path,
) -> tuple[list[str], list[dict[str, str]]]:
    """Write every secondary root's guide and queue the primary's regions.

    The primary is skipped on purpose: its guide arrived by ``git mv``, so
    rewriting it here would break both byte-identity and ``git log --follow`` for
    no gain.

    Returns ``(created, stashed)``. A queue file that already existed is stashed
    before being appended to, so undo restores it byte-for-byte; one that did not
    exist is recorded as created, so undo removes it. Both are needed — the
    reference install has a `Memory-Proposals.md` in both workspaces, and a fresh
    root has none.
    """
    from ciao.memory_proposals import append_proposals  # noqa: PLC0415

    install_root = Path(install_root)
    created: list[str] = []
    stashed: list[dict[str, str]] = []

    for name, text in sorted(split.per_root.items()):
        if name == split.primary:
            continue
        root = install_root / name
        root.mkdir(parents=True, exist_ok=True)
        guide = root / "CLAUDE.md"
        if not guide.exists():
            guide.write_text(text, encoding="utf-8")
            created.append(f"{name}/CLAUDE.md")

        proposals = split.queued_proposals.get(name) or []
        destination = vault_destinations.get(name)
        if not proposals or not destination:
            continue
        queue = install_root / destination / _QUEUE_RELATIVE
        if queue.is_file():
            # Keyed on the full relative path, never the stem: both workspaces
            # hold a Memory-Proposals.md, so a flat backup name would have one
            # root's queue overwrite the other's.
            target = backup_dir / destination / _QUEUE_RELATIVE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(queue.read_bytes())
            stashed.append(
                {
                    "source": f"{destination}/{_QUEUE_RELATIVE}",
                    "backup": str(target.relative_to(runtime_root)),
                }
            )
        written = append_proposals(
            proposals, install_root / destination, source_path=Path("CLAUDE.md")
        )
        if written is not None and not stashed_holds(stashed, destination):
            created.append(f"{destination}/{_QUEUE_RELATIVE}")
    return created, stashed


def stashed_holds(stashed: list[dict[str, str]], destination: str) -> bool:
    """Whether this run already stashed that root's queue file."""
    wanted = f"{destination}/{_QUEUE_RELATIVE}"
    return any(entry["source"] == wanted for entry in stashed)


def guide_split_pending(root: Path) -> bool:
    """Whether this root has no guide while the install root still holds one.

    One definition, honoured by the bootstrap and reported by ``--repair``.
    Seeding the packaged stock guide in that state replaces the user's guide with
    empty memory regions, so nothing may seed one until the split has run. During
    a migration the shared guide has already moved into the primary root, so this
    is false for every root and the bootstrap proceeds normally.
    """
    root = Path(root)
    return not (root / "CLAUDE.md").is_file() and (root.parent / "CLAUDE.md").is_file()


def bootstrap_root(root: Path, shared: Path) -> tuple[list[str], list[str]]:
    """Install the agent assets a root needs to be usable. Safe to run twice.

    Called by both the migration and ``--repair``, so "after a migration,
    ``--repair`` is a no-op" is an invariant rather than a coincidence. Without
    this, ``apply`` produced roots holding a vault and nothing else: no
    ``.claude/``, no ``AGENTS.md``, so a session in that root discovered no
    skill, agent or command at all.

    Returns ``(created files, created directories)`` relative to ``root``, so
    undo removes exactly what this added. The directory list exists because
    ``.claude/`` holds hundreds of packaged files and recording each one in the
    receipt would bury the reverse map in noise.
    """
    from ciao.sync_skills import (  # noqa: PLC0415
        _ensure_linked_workspace_guides,
        _install_stock_agents,
        _install_stock_skills,
        _mirror_dir_symlinks,
        _rebuild_custom_skill_links,
        _seed_stock_commands,
        mirror_shared_skill_sources,
    )

    root = Path(root)
    before = {name: (root / name).exists() for name in _GUIDE_NAMES}
    # Directories the bootstrap may create. `.claude/` holds the generated
    # mirrors; `commands/` is seeded with the packaged stock commands, and
    # `subagents/` may be created by the mirror step. Undo removes whichever of
    # them were not there beforehand, or a round trip leaves the seeded stock
    # commands behind in roots that no longer exist.
    _BOOTSTRAP_DIRS = (".claude", "commands", "subagents")
    dirs_existed = {name: (root / name).is_dir() for name in _BOOTSTRAP_DIRS}

    # Skills and agents are always safe to install; a guide is not. See
    # `guide_split_pending`.
    if not guide_split_pending(root):
        _ensure_linked_workspace_guides(root)
    _install_stock_skills(root)
    _rebuild_custom_skill_links(root)
    mirror_shared_skill_sources(root, shared)
    # The provider-facing mirrors, which is what makes a moved `commands/` or
    # `subagents/` discoverable from the root that now holds it. Without them a
    # migrated root had a catalog on disk and nothing pointing at it.
    _seed_stock_commands(root)
    _mirror_dir_symlinks(
        root / "commands",
        root / ".claude" / "commands",
        glob_pattern="*.md",
        prune_regular=False,
    )
    _mirror_dir_symlinks(
        root / "subagents",
        root / ".claude" / "agents",
        glob_pattern="*.md",
        prune_regular=False,
    )
    # No guard here: like _install_stock_skills, this handles its own missing
    # package resources. A bare except would only hide a real failure.
    _install_stock_agents(root)

    created_files = [
        name
        for name in _GUIDE_NAMES
        if not before[name] and ((root / name).is_symlink() or (root / name).exists())
    ]
    created_dirs = [
        name
        for name in _BOOTSTRAP_DIRS
        if not dirs_existed[name] and (root / name).is_dir()
    ]
    return created_files, created_dirs


# Paths a snapshot commit must never capture. An auto-created repository is a
# safety net, not a publication: `.env` holds the PWA token and provider keys,
# `secrets/` holds operator credentials, and `.runtime/` is volatile state the
# migration writes into as it runs. None of them are moved by the migration, so
# excluding them costs the rollback nothing. `.env.example` is deliberately not
# matched — it is documentation.
_SNAPSHOT_IGNORES: tuple[str, ...] = (
    ".runtime/",
    ".env",
    ".credentials",
    "secrets/",
    "node_modules/",
    ".venv/",
    ".DS_Store",
    ".obsidian/workspace*",
)


def ensure_rollback_history(install_root: Path) -> dict[str, Any]:
    """Give the install a git history to roll back to, creating one if needed.

    Every move is a ``git mv``, and ``git checkout`` is the undo. An install
    whose vault is not in a repository therefore could not migrate at all: the
    first ``git mv`` failed and the run refused, permanently, while the blocking
    gate kept asking. Refusing was correct — losing the undo is worse than not
    migrating — but the missing piece is cheap to create, so create it.

    Three states, one outcome:

    - a repository with at least one commit: left completely alone.
    - a repository with no commits: given the snapshot commit, because ``git mv``
      works without a HEAD but ``git checkout`` has nothing to return to.
    - not a repository: ``git init``, a ``.gitignore``, then the snapshot.

    The snapshot deliberately excludes credentials and volatile state (see
    ``_SNAPSHOT_IGNORES``). A safety net that captured `.env` would turn "we made
    you a backup" into "we committed your provider keys".
    """
    root = Path(install_root).resolve()
    out: dict[str, Any] = {"status": "", "created_repo": False, "commit": ""}
    if shutil.which("git") is None:
        out["status"] = "no_git_binary"
        return out
    if not root.is_dir():
        out["status"] = "no_install_root"
        return out

    code, top = _run_git(root, "rev-parse", "--show-toplevel")
    inside = code == 0 and top.strip() != ""
    if inside:
        head_code, head = _run_git(root, "rev-parse", "HEAD")
        if head_code == 0 and head.strip():
            out["status"] = "existing"
            out["commit"] = head.strip()
            return out
        out["status"] = "seeded_empty_repo"
    else:
        _write_snapshot_gitignore(root)
        init_code, init_out = _run_git(root, "init", "-b", "main")
        if init_code != 0:
            out["status"] = "init_failed"
            out["error"] = init_out.strip()
            return out
        out["created_repo"] = True
        out["status"] = "created"

    add_code, add_out = _run_git(root, "add", "-A")
    if add_code != 0:
        out["status"] = "add_failed"
        out["error"] = add_out.strip()
        return out
    commit_code, commit_out = _run_git(
        root,
        "-c",
        "user.name=Ciaobot",
        "-c",
        "user.email=ciaobot@localhost",
        "commit",
        "-m",
        "Ciaobot: snapshot before the workspace re-rooting",
    )
    if commit_code != 0:
        out["status"] = "commit_failed"
        out["error"] = commit_out.strip()
        return out
    head_code, head = _run_git(root, "rev-parse", "HEAD")
    out["commit"] = head.strip() if head_code == 0 else ""
    return out


def _write_snapshot_gitignore(root: Path) -> None:
    """Add the exclusions a fresh snapshot needs, keeping any existing file."""
    path = root / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry in _SNAPSHOT_IGNORES if entry not in present]
    if not missing:
        return
    header = "" if existing else (
        "# Ciaobot: credentials and volatile state stay out of snapshots\n"
    )
    body = existing if not existing or existing.endswith("\n") else existing + "\n"
    path.write_text(header + body + "\n".join(missing) + "\n", encoding="utf-8")


# -- P10.6: rebuild the derived artefacts per root ---------------------------


def rebuild_indexes(
    install_root: Path, workspaces: list[str], *, vault_name: str = VAULT_DIR_NAME
) -> dict[str, Any]:
    """Rebuild each root's INDEX.md and VOCABULARY.md, with no path prefix.

    ``vault_name`` is the vault directory's leaf, which is configurable
    (``CIAO_VAULT_ROOT``) and is NOT always ``memory-vault``. Hardcoding it meant
    that on such an install every root's vault looked absent, so nothing was
    rebuilt — while ``plan()``, which derives the name correctly, had already
    moved the vaults and the receipt reported the migration as complete. The
    result was an install whose per-root ``INDEX.md`` simply did not exist, which
    reads to every consumer as "this workspace has no notes".

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
    leaf = vault_name or VAULT_DIR_NAME
    for name in workspaces:
        vault = Path(install_root) / name / leaf
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
    vault_name: str = VAULT_DIR_NAME,
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

    from ciao.fts_search import get_db_path, index_logs, index_vault, init_db

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
        leaf = vault_name or VAULT_DIR_NAME
        for name in workspaces:
            vault = Path(install_root) / name / leaf
            if not vault.is_dir():
                # Reported, not skipped silently. A vault that is not where this
                # expects it is the whole failure mode the `vault_name` parameter
                # exists for, and `continue` alone made it look like a clean run.
                result["errors"].append(
                    {"workspace": name, "error": f"no vault at {vault}"}
                )
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
        # The transcript archive is dropped along with everything else, so
        # rebuilding only the vaults left `transcript_meta` empty and every
        # transcript search silently answering nothing. It is not per-root: D5
        # PROMOTES `Logs/` to the install root unsplit, so it indexes once,
        # keyed against the same base as the vault rows.
        logs = Path(install_root) / "Logs"
        if logs.is_dir():
            try:
                indexed, removed = index_logs(
                    conn, logs.parent, logs_root=logs, path_base=install_root
                )
                result["logs"] = {"indexed": indexed, "removed": removed}
            except Exception as exc:  # noqa: BLE001
                result["errors"].append({"workspace": "", "error": str(exc)})
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
# Everything at the install root that is a user-authored agent asset, and so
# cannot be attributed to a workspace by anything but a human. `subagents/` and
# `commands/` are the same class as `skills/`: `sync_workspace_skills` mirrors
# each of them from the root it is syncing into that root's `.claude/`, so an
# asset left at the install root after the migration is mirrored into no root at
# all. Measured on the reference install: 21 skill entries and 3 commands.
_CATALOG_PATHS: tuple[str, ...] = (
    "skills",
    "commands",
    "subagents",
)

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

    ``skills/`` is the custom catalog, mirrored into ``.claude/skills`` by
    ``sync_skills._rebuild_custom_skill_links`` from whichever root it sits in.
    """
    install_root = Path(install_root).resolve()
    triage = SkillsTriage(primary=primary)
    if not primary:
        triage.refusals.append(
            "no primary workspace, so there is no root to hold the skill catalog"
        )
        return triage

    for relative in _CATALOG_PATHS:
        source = install_root / relative
        if not source.exists():
            continue
        # `git mv` refuses an empty directory ("fatal: source directory is
        # empty"), which would fail the whole run and roll it back. The reference
        # install has an empty `subagents/`, and an empty directory has nothing to
        # move anyway — the bootstrap creates one per root when it needs to.
        if source.is_dir() and not any(source.iterdir()):
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
                origin=str(skill.get("source") or "skills/"),
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
            "3. Run `ciao sync-skills` for both roots. The links under "
            "`.claude/skills` are rebuilt from `skills/`, so nothing else needs "
            "editing."
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


# -- The upgrade trigger ----------------------------------------------------


def migrate_if_needed(config: Any) -> dict[str, Any]:
    """Re-root this install once, at upgrade, before anything reads the vault.

    The design always intended this to run unattended at upgrade rather than
    from a command: an install nobody migrates gets a feature nobody reaches, and
    asking every user to run a CLI migration by hand is how one gets run against
    an engine that does not understand the result. Which is exactly what happened
    once already on the reference install.

    Retirement posture: once the receipt reads ``migrated`` — a real migration or
    a born-per-root install, which writes ``status: "migrated"`` with no moves —
    this is a NO-OP that touches nothing but the receipt file. The receipt check
    runs before any registry or vault-path work, so a migrated install never
    pays for planning, rehearsal, or even a full config read here. The module
    itself stays because installs migrated by earlier releases may still exist;
    deleting it before receipts can be assumed universal would strand them. The
    manual ``ciao workspace-reroot`` subcommands (rehearse/apply/undo/repair)
    remain available for recovery regardless of this gate.

    Never raises. A migration that cannot run is a condition to SURFACE, not a
    reason to fail an upgrade and leave the install half-started — the refusal is
    already recorded in the receipt, and the `workspace-unmigrated` action reads
    it back and offers the retry.

    Ordering matters and is the caller's job: after the git sync, so the
    clean-tree gate judges the real tree, and before the index refresh, so the
    indexes are rebuilt for the layout that now exists.
    """
    outcome: dict[str, Any] = {"status": "skipped"}
    try:
        install_root = Path(config.workspace_root)
        # Same fallback `DetectionContext.runtime` uses, so this is callable from
        # any config-like object rather than only a full CiaoConfig.
        state_path = getattr(config, "state_path", None)
        runtime_root = (
            Path(state_path).parent if state_path else install_root / ".runtime"
        )
    except Exception as exc:  # noqa: BLE001 — a broken config is not our error
        logger.exception("re-root: could not read the install layout")
        return {"status": "error", "reason": str(exc)}

    if read_receipt(runtime_root) is not None:
        return {"status": "already_migrated"}

    try:
        vault_root = Path(config.vault_root)
        names = sorted(n for n in config.workspace_names() if n)
        primary = config.primary_workspace()
    except Exception as exc:  # noqa: BLE001 — a broken config is not our error
        logger.exception("re-root: could not read the install layout")
        return {"status": "error", "reason": str(exc)}
    if not names or not primary:
        # Nothing registered yet: a fresh install still in setup. There is no
        # root to create and nothing to move.
        return {"status": "not_applicable", "reason": "no registered workspace"}
    if not vault_root.is_dir():
        return {"status": "not_applicable", "reason": f"no vault at {vault_root}"}

    try:
        outcome = apply(install_root, vault_root, names, runtime_root, primary=primary)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.exception("re-root: apply failed")
        return {"status": "error", "reason": str(exc)}

    if outcome.get("status") != "migrated":
        logger.warning(
            "re-root: refused, install stays on the shared layout (%s)",
            "; ".join(outcome.get("refusals") or ["no reason recorded"]),
        )
        return outcome

    # Derived state, rebuilt for the layout that now exists. Reported per root
    # and never fatal: a failed index rebuild is a stale index, not a lost vault,
    # and the migration itself has already succeeded and been recorded.
    # The leaf comes from the vault that was actually moved, not from a constant:
    # `plan()` moves `<vault>` to `<root>/<name>/<leaf>`, so the rebuilds have to
    # look for the same leaf or they find nothing on any install whose
    # CIAO_VAULT_ROOT does not end in `memory-vault`.
    leaf = vault_root.name or VAULT_DIR_NAME
    try:
        outcome["indexes"] = rebuild_indexes(install_root, names, vault_name=leaf)
        outcome["search"] = rebuild_search_index(install_root, names, vault_name=leaf)
    except Exception as exc:  # noqa: BLE001
        logger.exception("re-root: rebuilding derived state failed")
        outcome["derived_error"] = str(exc)
    logger.info(
        "re-root: migrated %d workspace(s); %d chat(s) will start fresh provider sessions",
        len(names),
        len((outcome.get("stranded_sessions") or {}).get("flagged") or []),
    )
    return outcome


# -- P10.11: idempotent reconciliation to the registry -----------------------

# The registry is the authority on which roots must exist and where each keeps
# its vault. `repair` re-derives the intended layout from it and reconciles the
# filesystem to it, changing nothing when already correct. Everything it fixes is
# derived or structural: a missing directory, an unlinked guide, an un-mirrored
# shared skill, a stale index. Nothing that carries the user's content or their
# credentials is rewritten, which is why the two entries below are reported
# rather than repaired.
_REPAIR_REPORT_ONLY = (
    # A root with no vault. Which notes belong to it is a question about the
    # user's own material, so guessing would re-create the misfiling this
    # release repairs.
    "vault_missing",
    # A root with no guide while the install root still holds the pre-migration
    # one. Seeding the packaged stock guide there would replace the user's guide
    # with empty memory regions; splitting it is P10.4's job, not a repair's.
    "guide_unsplit",
    # `.mcp.json` grants live credentialed access. Recomposing it from a shared
    # source is a design decision with real blast radius, and per-root
    # composition does not exist yet — the earlier attempt at inferring MCP
    # reachability silently removed two working integrations from a live install.
    "mcp_drift",
)


@dataclass(frozen=True, slots=True)
class RepairItem:
    """One piece of drift, and what was done about it."""

    workspace: str
    drift: str
    detail: str
    action: str


def repair(
    install_root: Path,
    runtime_root: Path,
    workspaces: list[str],
    *,
    shared_sources: Path | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Reconcile the filesystem to the registry. Idempotent; a no-op when correct.

    Refuses on an install that has not re-rooted, and the reason is not caution:
    before the migration the shared ``INDEX.md`` legitimately carries workspace
    prefixes, and that prefix is what ``_entity_visible_in_workspace`` filters
    on. "Repairing" it there would strip the prefixes and leave no filter over
    the index, making every entity visible in every session. That is the same
    fail-open state the deletions in P10.9 are gated on, reached from the other
    direction.
    """
    install_root = Path(install_root).resolve()
    runtime_root = Path(runtime_root)
    receipt = read_receipt(runtime_root)
    # The leaf the migration actually used, recorded in its own receipt. Assuming
    # `memory-vault` made repair report `vault_missing` for every root of an
    # install with a different CIAO_VAULT_ROOT leaf — a false alarm about the one
    # thing repair exists to detect.
    leaf = Path(str((receipt or {}).get("vault_root", ""))).name or VAULT_DIR_NAME
    if receipt is None:
        return {
            "status": "not_rerooted",
            "reason": (
                "this install has not re-rooted, so there is no per-root layout to "
                "reconcile; run `ciao workspace-reroot --apply` first"
            ),
            "repaired": [],
            "reported": [],
            "errors": [],
        }

    shared = Path(shared_sources) if shared_sources else install_root / _SKILLS_SRC
    repaired: list[RepairItem] = []
    reported: list[RepairItem] = []
    errors: list[dict[str, str]] = []

    def record(item: RepairItem) -> None:
        (reported if item.drift in _REPAIR_REPORT_ONLY else repaired).append(item)

    for name in sorted(workspaces):
        root = install_root / name
        try:
            _repair_one_root(root, name, shared, record, vault_name=leaf)
        except Exception as exc:  # noqa: BLE001 — one bad root must not stop the rest
            errors.append({"workspace": name, "error": str(exc)})

    # The search index is derived and install-wide, so it is checked once. A row
    # whose path no longer resolves is not stale, it is wrong: the note it points
    # at moved when the vault did.
    try:
        stale = stale_search_rows(install_root, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        stale = []
        errors.append({"workspace": "", "error": f"could not inspect the search index: {exc}"})
    try:
        unindexed = unindexed_transcript_archive(install_root, db_path=db_path)
    except Exception as exc:  # noqa: BLE001
        unindexed = 0
        errors.append(
            {"workspace": "", "error": f"could not inspect the transcript index: {exc}"}
        )
    if stale or unindexed:
        rebuilt = rebuild_search_index(
            install_root, sorted(workspaces), db_path=db_path, vault_name=leaf
        )
        errors.extend(
            {"workspace": e.get("workspace", ""), "error": e.get("error", "")}
            for e in rebuilt.get("errors", [])
        )
        if stale:
            repaired.append(
                RepairItem(
                    workspace="",
                    drift="search_index_stale",
                    detail=(
                        f"{len(stale)} indexed path(s) no longer resolve, "
                        f"e.g. {stale[0]}"
                    ),
                    action="dropped and rebuilt the search index",
                )
            )
        if unindexed:
            repaired.append(
                RepairItem(
                    workspace="",
                    drift="transcript_index_empty",
                    detail=f"{unindexed} transcript(s) in Logs/ were not indexed",
                    action="dropped and rebuilt the search index",
                )
            )

    return {
        "status": "repaired" if repaired else "clean",
        "repaired": [asdict(item) for item in repaired],
        "reported": [asdict(item) for item in reported],
        "errors": errors,
    }


def _repair_one_root(
    root: Path,
    name: str,
    shared: Path,
    record: Any,
    *,
    vault_name: str = VAULT_DIR_NAME,
) -> None:
    """Reconcile one agent root. Every branch is safe to run twice."""
    from ciao.sync_skills import _ensure_linked_workspace_guides  # noqa: PLC0415

    if not root.is_dir():
        root.mkdir(parents=True, exist_ok=True)
        bootstrap_root(root, shared)
        record(
            RepairItem(
                workspace=name,
                drift="root_missing",
                detail=f"registered workspace has no directory at {root}",
                action="created the root and installed its agent assets",
            )
        )

    vault = root / (vault_name or VAULT_DIR_NAME)
    if not vault.is_dir():
        record(
            RepairItem(
                workspace=name,
                drift="vault_missing",
                detail=f"the root exists but holds no vault at {vault}",
                action=(
                    "reported, not created: which notes belong to this workspace "
                    "is a question about the user's own material"
                ),
            )
        )

    guide = root / "CLAUDE.md"
    agents = root / "AGENTS.md"
    shared_guide = root.parent / "CLAUDE.md"
    if guide_split_pending(root):
        # Refuse to seed a stock guide over an unsplit one. This root has no
        # guide and the install root still holds the pre-migration one, so
        # `_ensure_linked_workspace_guides` would copy the ~2 KB packaged stock
        # guide with EMPTY memory regions while the user's real guide sits
        # orphaned at a path no session's cwd reads. That turns a missing step
        # into silent loss of every remembered fact, so it is reported instead.
        record(
            RepairItem(
                workspace=name,
                drift="guide_unsplit",
                detail=(
                    f"{root} has no CLAUDE.md and {shared_guide} still holds the "
                    "pre-migration guide, so the split has not run"
                ),
                action=(
                    "reported, not seeded: writing the packaged stock guide here "
                    "would replace the user's guide with empty memory regions"
                ),
            )
        )
    else:
        linked = agents.is_symlink() and (root / os.readlink(agents)).resolve() == guide.resolve()
        if not linked:
            _ensure_linked_workspace_guides(root)
            if agents.is_symlink() or agents.exists():
                record(
                    RepairItem(
                        workspace=name,
                        drift="agents_unlinked",
                        detail=f"{agents} did not resolve to {guide}",
                        action="re-linked AGENTS.md to this root's CLAUDE.md",
                    )
                )

    # Read the drift BEFORE mirroring, or the report always comes back empty and
    # a genuine repair looks like a no-op.
    missing = _missing_skill_links(root, shared)
    bootstrap_root(root, shared)
    if missing:
        record(
            RepairItem(
                workspace=name,
                drift="skills_unmirrored",
                detail=f"{len(missing)} skill(s) were not linked into the catalog: "
                + ", ".join(missing[:5]),
                action=f"re-mirrored the catalog, restoring {len(missing)} link(s)",
            )
        )

    if not (root / ".mcp.json").is_file() and (shared / ".mcp.json").is_file():
        record(
            RepairItem(
                workspace=name,
                drift="mcp_drift",
                detail=f"a shared {shared / '.mcp.json'} exists and this root has none",
                action=(
                    "reported, not composed: an MCP entry grants credentialed "
                    "access, and per-root composition is not built yet"
                ),
            )
        )

    index = vault / "INDEX.md"
    prefixed = index_workspace_prefixes(index, [name])
    if vault.is_dir() and (not index.is_file() or prefixed):
        rebuild_indexes(root.parent, [name], vault_name=vault.name)
        record(
            RepairItem(
                workspace=name,
                drift="index_prefixed" if prefixed else "index_missing",
                detail=(
                    f"{index} still keys {len(prefixed)} entry path(s) under a "
                    "workspace name" if prefixed else f"{index} is absent"
                ),
                action="rebuilt this root's INDEX.md and VOCABULARY.md",
            )
        )


def _missing_skill_links(root: Path, shared: Path) -> list[str]:
    """Skills that should be linked into this root's catalog and were not.

    Checked BEFORE the mirroring functions run, so the report describes the drift
    that was found rather than the state that was written. Reading it after would
    always come back empty and the repair would look like a no-op.

    A shared-source skill counts as drift when the catalog entry is not already a
    link to it, not merely when the NAME is absent. Several shared skills carry
    the same name as a packaged stock skill they are meant to override, so a
    name-only check reported nothing while the repair silently replaced a stock
    copy with the shared link.
    """
    claude_skills = root / ".claude" / "skills"
    present = {entry.name for entry in _iter_dir(claude_skills)}
    drift: set[str] = set()
    for entry in _iter_dir(root / "skills"):
        if entry.is_dir() and (entry / "SKILL.md").is_file() and entry.name not in present:
            drift.add(entry.name)
    own_names = {entry.name for entry in _iter_dir(root / "skills")}
    for entry in _iter_dir(shared):
        if not entry.is_dir() or not (entry / "SKILL.md").is_file():
            continue
        if entry.name in own_names:
            continue  # the root's own copy wins, so the shared one is not drift
        target = claude_skills / entry.name
        try:
            linked = target.is_symlink() and target.resolve() == entry.resolve()
        except OSError:
            linked = False
        if not linked:
            drift.add(entry.name)
    return sorted(drift)


def _iter_dir(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return []


def index_workspace_prefixes(index: Path, workspaces: list[str]) -> list[str]:
    """Entry paths in an INDEX.md that still start with a workspace name.

    Before the migration every entry is keyed ``personal/People/Foo.md`` inside
    one shared vault. After it, each root holds exactly one vault, so a prefix
    has nothing left to disambiguate and its presence is a false statement about
    where the note lives.
    """
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return []
    names = {name for name in workspaces if name}
    found: list[str] = []
    for line in text.splitlines():
        if not line.startswith("- ["):
            continue
        label = line[3 : line.find("]")] if "]" in line else ""
        head = label.split("/", 1)[0]
        if head in names:
            found.append(label)
    return found


def unindexed_transcript_archive(
    install_root: Path, *, db_path: Path | None = None
) -> int:
    """Files in the promoted archive when the transcript index holds nothing.

    `stale_search_rows` answers "do the indexed paths still resolve" — which a
    table with no rows at all passes trivially. So a rebuild that dropped the
    database and re-indexed only the vaults left transcript search empty and
    every check downstream called it clean. The count of what should have been
    indexed is the signal; zero archive files means there is nothing to report.
    """
    import sqlite3

    from ciao.fts_search import get_db_path

    install_root = Path(install_root).resolve()
    logs = install_root / "Logs"
    if not logs.is_dir():
        return 0
    db = Path(db_path) if db_path is not None else get_db_path()
    if not db.exists():
        return 0
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT count(*) FROM transcript_meta").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
    if rows:
        return 0
    return sum(1 for _ in logs.rglob("*.md"))


def stale_search_rows(
    install_root: Path, *, db_path: Path | None = None, limit: int = 5
) -> list[str]:
    """Indexed paths that no longer resolve under the install root.

    Read only, and bounded: it stops at ``limit`` because the repair only needs
    to know whether a rebuild is due and one example to name, not the whole list.
    """
    import sqlite3

    from ciao.fts_search import get_db_path

    db = Path(db_path) if db_path is not None else get_db_path()
    if not db.exists():
        return []
    install_root = Path(install_root).resolve()
    conn = sqlite3.connect(db)
    stale: list[str] = []
    try:
        for (path,) in conn.execute("SELECT path FROM vault_meta"):
            if not (install_root / str(path)).exists():
                stale.append(str(path))
                if len(stale) >= limit:
                    break
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
    return stale
