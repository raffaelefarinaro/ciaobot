"""Move one workspace's vault to its standard folder.

Distinct from ``workspace_reroot``, which re-architects EVERY registered
workspace into its own agent root in one shot. This handles the narrower,
common case the "vault is not in its standard folder" housekeeping card
flags: one workspace's vault sits at a non-standard path — usually because it
was adopted before the per-workspace subfolder convention, or hand-pointed at
an external directory — and needs to move to
``CiaoConfig.canonical_workspace_vault_root(name)``. Only that one
workspace's registry entry is touched.

Two shapes, both handled:

- the vault lives at some other path entirely (an absolute pinned root, or a
  legacy one-segment value outside the vault) — the whole directory is this
  workspace's own content, so it moves as a unit.
- the vault IS the shared vault root itself — a workspace adopted before
  per-workspace subfolders got everything at the top level, alongside other
  workspaces' already-nested folders and vault-wide shared state (Logs,
  Templates, the generated indexes). Each top-level entry is classified
  rather than moving the root itself, and a symlink or an entry this module
  cannot place is left ``unclassified`` — never guessed — so the caller (a
  chat agent, in practice) only has to ask about those, not about the move
  as a whole.

Every move is a ``git mv``, so ``git mv`` in reverse (what :func:`undo` does)
stays a working undo and history follows the file. Refuses before touching
anything if the plan refuses or a tracked file under the source has
uncommitted changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.workspace_reroot import (
    _run_git,
    _write_registry,
    dirty_tracked_paths,
    ensure_rollback_history,
    registry_file,
)

logger = logging.getLogger(__name__)

RECEIPT_VERSION = 1

# Content that belongs to the shared vault as a whole, not to any one
# workspace. Moving these into a workspace's own folder would misfile shared
# state as that workspace's content. Mirrors workspace_reroot's own
# global/regenerated split, applied one level up: at vault_root's own
# children, not at vault_root/<name>'s.
_SHARED_NAMES: frozenset[str] = frozenset(
    {
        "Logs",
        "Templates",
        ".obsidian",
        "INDEX.md",
        "MEMORY.md",
        "VOCABULARY.md",
        ".DS_Store",
        ".runtime",
    }
)

EXTERNAL_VAULT_REFUSAL = (
    "the vault lives outside the install's git worktree (an external or "
    "hand-pinned vault root), so it cannot be moved with git mv and there "
    "is no automatic undo for it here; back it up, move it by hand, then "
    "update the workspace registry and verify note counts before removing "
    "the backup"
)


@dataclass(frozen=True, slots=True)
class RelocationEntry:
    """One top-level name under the shared vault root, and what to do with it."""

    name: str
    action: str  # "move" | "skip" | "unclassified"
    reason: str


@dataclass(slots=True)
class RelocationPlan:
    """What the relocation would do, and why it would refuse. Never writes."""

    workspace: str
    source: str
    destination: str
    whole_directory: bool = False
    entries: list[RelocationEntry] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return bool(self.refusals) or any(
            entry.action == "unclassified" for entry in self.entries
        )

    @property
    def moves(self) -> list[RelocationEntry]:
        return [entry for entry in self.entries if entry.action == "move"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RECEIPT_VERSION,
            "workspace": self.workspace,
            "source": self.source,
            "destination": self.destination,
            "whole_directory": self.whole_directory,
            "entries": [asdict(entry) for entry in self.entries],
            "refusals": list(self.refusals),
            "refused": self.refused,
        }


def _at_or_beneath(container: Path, other: Path) -> bool:
    """Whether ``other`` is ``container`` itself or nested somewhere under it."""
    return container == other or container in other.parents


def plan(config: Any, workspace: str) -> RelocationPlan:
    """Classify the move for one workspace's vault. Read only."""
    try:
        source = Path(config.workspace_vault_root(workspace)).resolve()
        destination = Path(config.canonical_workspace_vault_root(workspace)).resolve()
    except ValueError as exc:
        result = RelocationPlan(workspace=workspace, source="", destination="")
        result.refusals.append(str(exc))
        return result

    result = RelocationPlan(
        workspace=workspace, source=str(source), destination=str(destination)
    )

    if source == destination:
        result.refusals.append("vault is already at its standard location")
        return result
    if source.is_symlink() or destination.is_symlink():
        result.refusals.append("neither the source nor the destination may be a symlink")
        return result
    if not source.is_dir():
        result.refusals.append(f"vault is not a directory: {source}")
        return result
    if destination.is_dir() and any(destination.iterdir()):
        result.refusals.append(
            f"destination already exists and is not empty: {destination}"
        )
        return result

    try:
        install_root = Path(config.workspace_root).resolve()
    except Exception:  # noqa: BLE001 — advisory; a bad config must not crash the plan
        install_root = None
    if source == install_root:
        # The documented existing-folder setup (CIAO_VAULT_ROOT and the
        # workspace's own vault_root both "."): the vault root IS the whole
        # install, so its top level mixes vault content with install control
        # files (CLAUDE.md, .git, .runtime). Nothing here can tell those
        # apart safely, so this shape refuses rather than moving CLAUDE.md
        # into a workspace folder.
        result.refusals.append(
            "the vault root is the install root itself (an existing-folder "
            "setup); this shape is not safe to classify automatically — "
            "relocate it by hand"
        )
        return result

    try:
        vault_root = Path(config.vault_root).resolve()
    except Exception:  # noqa: BLE001 — advisory; a bad config must not crash the plan
        vault_root = None

    try:
        all_names = list(config.workspace_names())
    except Exception:  # noqa: BLE001 — advisory; a bad config must not crash the plan
        all_names = [workspace]

    other_roots_by_name: dict[str, Path] = {}
    for name in all_names:
        if name == workspace:
            continue
        try:
            other_roots_by_name[name] = Path(config.workspace_vault_root(name)).resolve()
        except (ValueError, OSError):
            continue
    other_vault_roots = set(other_roots_by_name.values())

    if source != vault_root:
        # The whole directory is this workspace's own content — UNLESS
        # another registered workspace's vault is nested inside it (a pinned
        # legacy root can itself contain another workspace's subfolder, the
        # same overlap the shared-root branch below guards against). Moving
        # it wholesale would carry that workspace's notes along while only
        # this workspace's registry entry gets repointed, stranding the
        # other one at a path that just vanished.
        overlapping = sorted(
            name for name, root in other_roots_by_name.items() if _at_or_beneath(source, root)
        )
        if overlapping:
            result.refusals.append(
                "this vault contains another registered workspace's vault "
                f"({', '.join(overlapping)}); moving it whole would carry "
                "that workspace's notes along while only this workspace's "
                "registry entry gets repointed"
            )
            return result
        if _at_or_beneath(source, destination):
            # A whole-directory move whose destination lives inside the
            # source (a global vault root nested under the pinned vault) is
            # not a rename at all: `git mv` necessarily fails with "cannot
            # move a directory into itself", after the destination parents
            # have already been created inside the source. The shared-root
            # branch below never hits this — there the move is per-entry,
            # and the destination is a new subfolder of the source by
            # construction. Refuse at plan time instead.
            result.refusals.append(
                f"the canonical destination ({destination}) is at or beneath "
                "the vault being moved; relocating would nest the vault "
                "inside itself — repoint the workspace's vault_root first"
            )
            return result
        result.whole_directory = True
        return result

    # source IS the shared vault root: only this workspace's own content lives
    # loose at its top level, alongside other workspaces' already-nested
    # folders and vault-wide shared state.

    # A legacy install can have MORE THAN ONE workspace pinned to the shared
    # vault root itself (CiaoConfig.legacy_entity_workspace treats this as
    # ambiguous ownership too). Classifying loose entries as "this
    # workspace's own content" would hand every one of them to whichever
    # workspace is relocated first, stranding the other owner(s) — so this
    # refuses rather than guessing who owns what.
    owners = []
    for name in all_names:
        try:
            if Path(config.workspace_vault_root(name)).resolve() == vault_root:
                owners.append(name)
        except (ValueError, OSError):
            continue
    if len(owners) > 1:
        result.refusals.append(
            "the shared vault root is claimed by more than one workspace "
            f"({', '.join(sorted(owners))}); ownership of its loose top-level "
            "content is ambiguous, so nothing was classified"
        )
        return result

    for entry in sorted(source.iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_symlink():
            result.entries.append(RelocationEntry(name, "unclassified", "symlink"))
        elif any(_at_or_beneath(entry.resolve(), other_root) for other_root in other_vault_roots):
            result.entries.append(RelocationEntry(name, "skip", "belongs to another workspace"))
        elif entry == destination:
            result.entries.append(RelocationEntry(name, "skip", "canonical destination"))
        elif name in _SHARED_NAMES:
            result.entries.append(RelocationEntry(name, "skip", "shared across every workspace"))
        else:
            result.entries.append(RelocationEntry(name, "move", "this workspace's own content"))

    return result


def receipt_path(runtime_root: Path, workspace: str) -> Path:
    return Path(runtime_root) / "migration" / f"vault-relocate-{workspace}.json"


def _peek_receipt(runtime_root: Path, workspace: str) -> dict[str, Any] | None:
    """The receipt file whatever its status. See ``workspace_reroot.peek_receipt``."""
    path = receipt_path(runtime_root, workspace)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _write_receipt(runtime_root: Path, workspace: str, payload: dict[str, Any]) -> Path:
    """Persist a receipt atomically, keeping any earlier one beside it.

    Its own file, separate from ``workspace_reroot``'s ``workspace-rooting.json``
    receipt: that one gates on and never downgrades a ``status: "migrated"``
    full-install migration, and writing this operation's outcome into it would
    either collide with that state or be silently dropped by its guard.

    Never downgrades a completed ``status: "relocated"`` receipt with a
    non-relocated payload: retrying ``--apply`` after a successful run
    correctly refuses ("already at its standard location"), and writing that
    refusal over the completed receipt would make ``ciao vault-relocate
    <name> --undo`` a no-op for a relocation that is still fully in effect.
    """
    path = receipt_path(runtime_root, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if payload.get("status") != "relocated":
        existing = _peek_receipt(runtime_root, workspace)
        if existing is not None and existing.get("status") == "relocated":
            return path
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path.replace(path.with_name(f"{path.stem}.{stamp}{path.suffix}"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _read_registry(runtime_root: Path) -> list[dict[str, Any]] | None:
    path = registry_file(runtime_root)
    if not path.is_file():
        return None
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return entries if isinstance(entries, list) else None


def _registry_has_workspace(runtime_root: Path, workspace: str) -> bool:
    entries = _read_registry(runtime_root)
    return entries is not None and any(
        isinstance(entry, dict) and str(entry.get("name", "")) == workspace for entry in entries
    )


def _repoint_registry(
    runtime_root: Path, workspace: str, config: Any
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
    """Point one workspace's registry entry at its new vault_root.

    Reads and rewrites the raw list rather than round-tripping through
    ``WorkspaceConfig``, so an unknown key a future release adds survives an
    edit that only means to change one field — same reasoning as
    ``workspace_reroot._rewrite_registry``. Also updates the in-memory
    ``WorkspaceConfig`` on ``config`` so the CLI process that ran this sees the
    new location immediately; the running server, a separate process, does
    not — see the restart note ``apply`` attaches to its result.
    """
    entries = _read_registry(runtime_root)
    if entries is None:
        return None, None

    before = json.loads(json.dumps(entries))
    stored = config.stored_workspace_vault_root(workspace)
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name", "")) == workspace:
            entry["vault_root"] = stored
    _write_registry(runtime_root, entries)

    workspace_config = config.workspace(workspace)
    if workspace_config is not None:
        workspace_config.vault_root = stored
    return before, entries


def apply(
    config: Any,
    workspace: str,
    runtime_root: Path,
    *,
    plan_result: RelocationPlan | None = None,
    registry_authoritative: bool = True,
) -> dict[str, Any]:
    """Move one workspace's vault to its standard folder, and repoint the registry.

    Refuses before touching anything if the plan refuses, or a tracked file
    under the source has uncommitted changes (so ``git mv`` in reverse stays a
    working undo). On a mid-run failure, rolls back every move already made.

    ``registry_authoritative`` must reflect whether ``workspaces.json`` is
    actually what ``config`` sourced its workspaces from — this module has no
    way to know that on its own. A caller building ``config`` with
    ``CiaoConfig.from_env`` should pass ``not effective_config_source.get(
    "CIAO_WORKSPACES", "").strip()``, using the SAME merged environment (env
    plus any ``.env`` override) that built ``config``, not the raw ambient
    process environment, which can disagree with it.
    """
    result = plan_result if plan_result is not None else plan(config, workspace)
    payload = result.as_dict()
    payload["recorded_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    if result.refused:
        payload["status"] = "refused"
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    install_root = Path(config.workspace_root).resolve()
    source = Path(result.source)
    destination = Path(result.destination)
    if not source.is_relative_to(install_root) or not destination.is_relative_to(install_root):
        payload["status"] = "refused"
        payload["refusals"] = [EXTERNAL_VAULT_REFUSAL]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload
    history = ensure_rollback_history(install_root)
    payload["git_history"] = history
    if history["status"] in {"no_git_binary", "init_failed", "add_failed", "commit_failed"}:
        payload["status"] = "refused"
        payload["refusals"] = [
            "the install has no git history to roll back to and one could not "
            f"be created ({history.get('error') or history['status']}); every "
            "move is a git mv and git mv in reverse is the undo, so relocating "
            "without it would be unrecoverable"
        ]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    # The move is about to happen; if the result cannot be persisted, refuse
    # before touching anything rather than moving files nothing can find
    # again. A non-authoritative registry most often means this install's
    # workspaces come from CIAO_WORKSPACES (an env var) rather than
    # workspaces.json.
    if not registry_authoritative or not _registry_has_workspace(runtime_root, workspace):
        payload["status"] = "refused"
        payload["refusals"] = [
            f"no entry for '{workspace}' in the workspace registry "
            f"({registry_file(runtime_root)}); the new location could not be "
            "recorded, so nothing was moved. If this install configures "
            "workspaces via CIAO_WORKSPACES, repoint it by hand after moving "
            "the vault yourself."
        ]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    try:
        source_rel = str(source.relative_to(install_root))
    except ValueError:
        source_rel = None
    try:
        dest_rel = str(destination.relative_to(install_root))
    except ValueError:
        dest_rel = None

    # An external or hand-pinned vault root (CiaoConfig explicitly supports an
    # absolute vault_root outside the install) cannot be moved with git mv,
    # and there is no automatic undo for a move git cannot track. Refusing is
    # deliberate, not a gap to route around: this is the one case where a
    # careful manual move, backed up first, is the right tool.
    moves_to_apply: list[tuple[str, str]] = []
    if result.whole_directory:
        if source_rel is None or dest_rel is None:
            payload["status"] = "refused"
            payload["refusals"] = [EXTERNAL_VAULT_REFUSAL]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload
        moves_to_apply.append((source_rel, dest_rel))
    else:
        if source_rel is None or dest_rel is None:
            payload["status"] = "refused"
            payload["refusals"] = [EXTERNAL_VAULT_REFUSAL]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload
        for entry in result.moves:
            moves_to_apply.append((f"{source_rel}/{entry.name}", f"{dest_rel}/{entry.name}"))

    dirty = dirty_tracked_paths(install_root, *(m[0] for m in moves_to_apply))
    if dirty:
        payload["status"] = "refused"
        first = dirty[0]
        others = f" (and {len(dirty) - 1} more)" if len(dirty) > 1 else ""
        payload["refusals"] = [
            f"{first}{others} has uncommitted changes; commit or stash it so "
            "git mv in reverse stays a working undo"
        ]
        payload["dirty_tracked"] = dirty[:20]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload

    # git mv rejects directories containing only untracked files. Stage the
    # planned sources so new notes move through the same tracked, undoable path.
    for src_rel, _dst_rel in moves_to_apply:
        code, out = _run_git(install_root, "add", "-A", "--", src_rel)
        if code != 0:
            payload["status"] = "refused"
            payload["refusals"] = [f"could not stage {src_rel}: {out}"]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload

    code, head = _run_git(install_root, "rev-parse", "HEAD")
    payload["git_head_before"] = head if code == 0 else ""

    if not result.whole_directory:
        destination.mkdir(parents=True, exist_ok=True)
    elif destination.is_dir() and not destination.is_symlink():
        # plan() only guaranteed EMPTY, not absent. `git mv source dest`
        # treats an existing directory as a container and nests source
        # inside it instead of renaming — removing the empty shell first
        # turns this back into an exact rename, and the undo (a reverse
        # git mv) recreates it exactly the same way.
        destination.rmdir()

    applied: list[dict[str, str]] = []
    for src_rel, dst_rel in moves_to_apply:
        (install_root / dst_rel).parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", src_rel, dst_rel)
        if code != 0:
            for done in reversed(applied):
                _run_git(install_root, "mv", done["destination"], done["source"])
            payload["status"] = "refused"
            payload["refusals"] = [f"git mv failed for {src_rel}: {out}"]
            payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
            return payload
        applied.append({"source": src_rel, "destination": dst_rel})

    source_removed = False
    if not result.whole_directory and source.is_dir() and not any(source.iterdir()):
        try:
            source.rmdir()
            source_removed = True
        except OSError:
            pass

    try:
        registry_before, registry_after = _repoint_registry(runtime_root, workspace, config)
    except OSError as exc:
        # The registry couldn't be read at all before any move ran (see the
        # earlier _registry_has_workspace refusal), but a write failure here
        # (permissions, disk full) shows up only now, with the vault already
        # moved on disk — leaving it moved with the registry unrepointed would
        # orphan it under a path nothing resolves to anymore, so unwind.
        for done in reversed(applied):
            _run_git(install_root, "mv", done["destination"], done["source"])
        if source_removed:
            source.mkdir(parents=True, exist_ok=True)
        if not result.whole_directory and destination.is_dir() and not any(destination.iterdir()):
            try:
                destination.rmdir()
            except OSError:
                pass
        payload["status"] = "refused"
        payload["refusals"] = [f"could not persist the new location in the registry: {exc}"]
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
        return payload
    payload["registry_before"] = registry_before
    payload["registry_after"] = registry_after
    payload["applied"] = applied
    payload["status"] = "relocated"
    # This CLI run's own in-memory config sees the new path immediately, but a
    # running Ciaobot server is a SEPARATE process holding its own CiaoConfig
    # built once at startup (ciao/main.py) — it keeps resolving the old
    # location, including in the very chat session that ran this command,
    # until it restarts. Surfaced here rather than restarted automatically:
    # the operator presses Restart, this command does not.
    payload["restart_required"] = True
    payload["restart_note"] = (
        "the running Ciaobot server will keep resolving the old location "
        "until it restarts (Settings -> Restart); tell the operator to "
        "restart before writing more notes through this workspace"
    )
    try:
        payload["receipt_path"] = str(_write_receipt(runtime_root, workspace, payload))
    except OSError as exc:
        for done in reversed(applied):
            (install_root / done["source"]).parent.mkdir(parents=True, exist_ok=True)
            _run_git(install_root, "mv", done["destination"], done["source"])
        if registry_before is not None:
            _write_registry(runtime_root, registry_before)
        payload["status"] = "refused"
        payload["refusals"] = [f"could not persist the relocation receipt: {exc}"]
        return payload
    return payload


def undo(
    config: Any, workspace: str, runtime_root: Path, *, registry_authoritative: bool = True
) -> dict[str, Any]:
    """Reverse the last completed relocation for one workspace, exactly.

    CLI only, same as ``workspace_reroot.undo``: there is no housekeeping
    button for this, only a receipt-driven reverse.

    See :func:`apply` for what ``registry_authoritative`` must reflect.
    """
    path = receipt_path(runtime_root, workspace)
    if not path.is_file():
        return {"status": "nothing_to_undo", "reason": "no relocation receipt for this workspace"}
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"status": "nothing_to_undo", "reason": "receipt is unreadable"}
    if not isinstance(receipt, dict) or receipt.get("status") != "relocated":
        return {"status": "nothing_to_undo", "reason": "last recorded run did not relocate anything"}

    install_root = Path(config.workspace_root).resolve()
    current_registry = _read_registry(runtime_root)
    if (
        not registry_authoritative
        or current_registry is None
        or not any(
            isinstance(entry, dict) and str(entry.get("name", "")) == workspace
            for entry in current_registry
        )
    ):
        return {
            "status": "refused",
            "reason": "the current workspace registry is missing, corrupt, or environment-authoritative; nothing was undone",
        }
    registry_before_undo = json.loads(json.dumps(current_registry))
    reversed_moves: list[str] = []
    already: list[str] = []
    applied_entries = receipt.get("applied", [])

    # Validate the entire reverse before moving anything. If a later source
    # path was recreated, reversing earlier entries first would leave a
    # partially undone relocation and make the retry harder to reason about.
    for entry in reversed(applied_entries):
        source = install_root / entry["source"]
        destination = install_root / entry["destination"]
        if not destination.exists() and not destination.is_symlink() and (
            source.exists() or source.is_symlink()
        ):
            already.append(entry["source"])
            continue
        if not source.exists() and not source.is_symlink() and not destination.exists() and not destination.is_symlink():
            return {
                "status": "refused",
                "reason": f"neither recorded path exists for {entry['source']}; nothing was moved",
                "reversed": reversed_moves,
                "already_reversed": already,
            }
        if source.exists() or source.is_symlink():
            return {
                "status": "refused",
                "reason": (
                    f"{entry['source']} already exists — something recreated "
                    "it since the relocation ran, so reversing would nest the "
                    "moved content inside it instead of restoring the "
                    "original layout; move or remove it, then retry"
                ),
                "reversed": reversed_moves,
                "already_reversed": already,
            }

    for entry in reversed(applied_entries):
        source = install_root / entry["source"]
        destination = install_root / entry["destination"]
        if not destination.exists() and not destination.is_symlink() and (
            source.exists() or source.is_symlink()
        ):
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        code, out = _run_git(install_root, "mv", entry["destination"], entry["source"])
        if code != 0:
            # Roll the earlier reversals forward again: the receipt and the
            # registry still describe the relocated layout, so leaving those
            # files back at their pre-relocation paths would strand them
            # outside the configured vault — the same unwind the apply path
            # does for partial moves.
            fail_reason = out
            rollback_error = None
            for done in reversed(reversed_moves):
                origin = next(
                    (
                        e
                        for e in applied_entries
                        if e["source"] == done
                    ),
                    None,
                )
                if origin is None:
                    continue
                (install_root / origin["source"]).parent.mkdir(
                    parents=True, exist_ok=True
                )
                code, out = _run_git(
                    install_root, "mv", origin["source"], origin["destination"]
                )
                if code != 0:
                    rollback_error = out
                    break
            reason = f"git mv failed reversing {entry['destination']}: {fail_reason}"
            if rollback_error:
                reason += f"; re-relocate also failed: {rollback_error}"
            return {
                "status": "failed",
                "reason": reason,
                "reversed": [],
                "already_reversed": already,
            }
        reversed_moves.append(entry["source"])
        parent = (install_root / entry["destination"]).parent
        if parent != install_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()

    # Only THIS workspace's vault_root is restored, into whatever the registry
    # currently holds — never the whole `registry_before` snapshot. Another
    # workspace can have been added, removed, or edited since the relocation
    # ran, and this command's scope is one workspace; overwriting the file
    # wholesale would silently discard those unrelated changes.
    before = receipt.get("registry_before")
    prior_entry = next(
        (e for e in (before or []) if isinstance(e, dict) and str(e.get("name", "")) == workspace),
        None,
    )
    if prior_entry is not None:
        prior_vault_root = prior_entry.get("vault_root", workspace)
        for entry in current_registry:
            if isinstance(entry, dict) and str(entry.get("name", "")) == workspace:
                entry["vault_root"] = prior_vault_root
        try:
            _write_registry(runtime_root, current_registry)
        except OSError as exc:
            rollback_error = None
            for entry in reversed(receipt.get("applied", [])):
                if entry["source"] in reversed_moves:
                    (install_root / entry["destination"]).parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    code, out = _run_git(install_root, "mv", entry["source"], entry["destination"])
                    if code != 0:
                        rollback_error = out
            try:
                _write_registry(runtime_root, registry_before_undo)
            except OSError as registry_exc:
                rollback_error = str(registry_exc)
            reason = f"could not restore the workspace registry: {exc}"
            if rollback_error:
                reason += f"; rollback also failed: {rollback_error}"
            return {
                "status": "refused",
                "reason": reason,
                "reversed": [],
                "already_reversed": already,
            }
        workspace_config = config.workspace(workspace)
        if workspace_config is not None and prior_vault_root is not None:
            workspace_config.vault_root = prior_vault_root

    path.unlink(missing_ok=True)
    return {
        "status": "undone",
        "reversed": reversed_moves,
        "already_reversed": already,
        "restart_required": True,
        "restart_note": "the running Ciaobot server must restart via Settings -> Restart before writing more notes",
    }
