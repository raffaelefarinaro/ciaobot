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
}

# Editor state for the vault as a whole, not for any one workspace. Promoted
# beside the workspaces rather than duplicated into each root.
_GLOBAL_KEEPS: frozenset[str] = frozenset({".obsidian"})

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
            elif name in _GLOBAL_KEEPS:
                result.global_keeps.append(f"{vault_root.name}/{name}")
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
