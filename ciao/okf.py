"""Export a vault (or one workspace of it) as a portable OKF bundle.

Why this exists at all
----------------------
After the link swap the vault *is* already a conformant Open Knowledge Format
bundle: markdown files with YAML frontmatter, a required `type` on every concept,
`index.md`/`log.md` as reserved names, and edges written as standard markdown
links. So this module is deliberately small — it does not convert anything. What
it adds is the one thing a folder cannot carry by itself:

* a **bundle-root `index.md`** with `okf_version`, which the spec allows only at
  the bundle root and not on every note, plus a table of contents so a consumer
  has an entry point;
* a **tarball**, so the bundle can be handed to someone as one artifact;
* **workspace scoping**, which is the case that actually comes up: sharing the
  work workspace with colleagues without handing over the personal one.

What it refuses to do
---------------------
Export an unmigrated vault silently. A bundle whose edges are `[[wikilinks]]` is
exactly the thing OKF exists to avoid — every consumer sees pages and no graph —
so a vault still in the old dialect is reported and, without ``--force``, not
written. That check is the whole reason the link swap came first.

Deliberately not implemented
----------------------------
The optional provenance fields (`generated`, `verified`, `status`, `sources`,
`stale_after`). They are additive frontmatter with no reader in this codebase,
and the plan that proposed them undercuts its own strongest argument: nothing
reads `updated:` for staleness today, so `stale_after` would not replace a
heuristic, it would invent enforcement. Adding keys nothing consumes makes the
schema look richer while changing nothing. `type: Attested Computation` is also
out: Ciaobot produces no attested computations, and half-modelling one is worse
than omitting it.
"""

from __future__ import annotations

import io
import logging
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.vault_index import Entry, scan_vault

logger = logging.getLogger(__name__)

# The spec revision this exporter targets. Written once, on the bundle-root
# `index.md` only — per SPEC, `okf_version` is a bundle-level key, so stamping it
# on every note would be wrong rather than merely redundant.
OKF_VERSION = "0.2"

BUNDLE_INDEX_NAME = "index.md"


def _path_scope(vault_root: Path, workspace: str) -> str:
    """The path segment to filter and strip, empty when there is nothing to strip.

    A workspace used to be a subtree of one shared vault, so scoping was purely a
    path filter on `Entry.path`. The per-workspace re-rooting broke that: a
    workspace-scoped invocation is handed `<install>/<name>/memory-vault`, whose
    scan yields `memory-vault/People/A.md` with no `<name>/` segment at all. The
    filter then dropped every entry and `ciao vault-export --workspace-name work`
    reported "no notes found for workspace 'work'" on every migrated install,
    writing no bundle.

    So: filter only when the workspace really is a subtree here. When instead the
    vault's own parent directory is the workspace, the vault IS the workspace and
    every note in it belongs in the bundle. An unknown workspace matches neither
    and still exports nothing, which is the correct refusal.
    """
    if not workspace:
        return ""
    if (Path(vault_root) / workspace).is_dir():
        return workspace  # legacy shared vault: the workspace is a subtree
    if Path(vault_root).parent.name == workspace:
        return ""  # re-rooted: this vault already holds only that workspace
    return workspace


def _bundle_entries(vault_root: Path, scope: str) -> list[Entry]:
    """Notes belonging in the bundle, in stable order.

    ``scope`` is the result of :func:`_path_scope`, not the raw workspace name:
    on a re-rooted install there is no segment to filter on.
    """
    entries = scan_vault(vault_root)
    if not scope:
        return sorted(entries, key=lambda item: str(item.path))
    prefix = f"{scope}/"
    kept = [
        entry
        for entry in entries
        if Path(str(entry.path)).relative_to(Path(str(entry.path)).parts[0])
        .as_posix()
        .startswith(prefix)
    ]
    return sorted(kept, key=lambda item: str(item.path))


def _relative_within_bundle(entry: Entry, workspace: str) -> str:
    """Path of a note inside the bundle, with the vault/workspace prefix removed.

    The workspace segment is stripped only when it is actually there: a re-rooted
    vault's entries never carry one, and an unconditional `relative_to` raised
    ValueError out of `format_bundle_index`.
    """
    relative = Path(str(entry.path))
    relative = relative.relative_to(relative.parts[0])  # drop the vault dir name
    if workspace and relative.parts[:1] == (workspace,):
        relative = relative.relative_to(workspace)
    return relative.as_posix()


def format_bundle_index(entries: list[Entry], workspace: str = "") -> str:
    """Render the bundle-root `index.md`.

    Grouped by type and linked with relative markdown links, which is what OKF's
    progressive-disclosure `index.md` is for: a consumer reads this one file and
    can reach everything else without knowing the layout.
    """
    scope = workspace or "vault"
    lines = [
        "---",
        f"okf_version: {OKF_VERSION}",
        "type: index",
        f"title: {scope}",
        f"description: Knowledge bundle exported from a Ciaobot vault ({scope}).",
        f"timestamp: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}",
        "---",
        "",
        f"# {scope}",
        "",
        f"{len(entries)} concepts. Every file carries a `type` in its frontmatter;",
        "links between concepts are relative markdown links.",
        "",
    ]
    by_type: dict[str, list[Entry]] = {}
    for entry in entries:
        by_type.setdefault(entry.type or "uncategorized", []).append(entry)
    for type_name in sorted(by_type):
        bucket = by_type[type_name]
        lines.append(f"## {type_name} ({len(bucket)})")
        lines.append("")
        for entry in sorted(bucket, key=lambda item: item.title.lower()):
            target = _relative_within_bundle(entry, workspace)
            label = entry.title or Path(target).stem
            lines.append(f"- [{label}](./{target})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def export_bundle(
    vault_root: Path,
    dest: Path,
    *,
    workspace: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Write a `.tar.gz` OKF bundle. Returns a summary; writes nothing on refusal.

    ``dest`` is the tarball path. ``workspace`` limits the bundle to one logical
    workspace, whose subtree becomes the bundle root — so intra-workspace links
    keep resolving, while any link that pointed into another workspace becomes
    dangling. Those are counted and reported rather than rewritten: silently
    editing a note on the way out would make the bundle disagree with the vault
    it came from.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "workspace": workspace,
        "dest": str(dest),
        "concepts": 0,
        "cross_workspace_links": 0,
        "written": False,
    }
    if not root.is_dir():
        summary["skipped"] = "vault root does not exist"
        return summary

    try:
        from ciao.vault_migrate_links import has_unmigrated_links

        unmigrated = has_unmigrated_links(root)
    except Exception:  # noqa: BLE001 — never fail an export over the advisory
        logger.exception("okf export: link-dialect check failed")
        unmigrated = ""
    if unmigrated and not force:
        summary["skipped"] = "vault still uses wikilinks"
        summary["example"] = unmigrated
        return summary

    # `workspace` stays the bundle's label; `scope` is the path segment that
    # exists in this layout, which on a re-rooted install is none.
    scope = _path_scope(root, workspace)
    entries = _bundle_entries(root, scope)
    summary["concepts"] = len(entries)
    if not entries:
        summary["skipped"] = (
            f"no notes found for workspace '{workspace}'" if workspace else "vault is empty"
        )
        return summary

    if scope:
        # A note in this workspace whose edge leaves it cannot resolve inside a
        # subtree bundle. Reported, not repaired. Only a subtree bundle can have
        # them: a re-rooted vault is exported whole, so every edge it resolved
        # stays inside the bundle root.
        prefix = f"{scope}/"
        for entry in entries:
            for ref in entry.related:
                inner = Path(str(ref))
                inner = inner.relative_to(inner.parts[0])
                if not inner.as_posix().startswith(prefix):
                    summary["cross_workspace_links"] += 1

    bundle_root = root / scope if scope else root
    # The label stays the workspace name even when there is no segment to strip:
    # `_relative_within_bundle` strips it only where the entries carry it.
    index_text = format_bundle_index(entries, workspace)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with tarfile.open(tmp, "w:gz") as archive:
            for entry in entries:
                relative = _relative_within_bundle(entry, workspace)
                archive.add(bundle_root / relative, arcname=relative)
            # The generated index replaces any INDEX.md the vault carries: that
            # one is a Ciaobot projection, this one is the bundle's entry point.
            info = tarfile.TarInfo(BUNDLE_INDEX_NAME)
            payload = index_text.encode("utf-8")
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
        tmp.replace(dest)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        summary["skipped"] = f"failed to write bundle: {exc}"
        return summary

    summary["written"] = True
    summary["okf_version"] = OKF_VERSION
    return summary


def bundle_members(dest: Path) -> list[str]:
    """Names inside an exported bundle. Used by tests and by `--list`."""
    with tarfile.open(Path(dest), "r:gz") as archive:
        return sorted(archive.getnames())


__all__ = [
    "BUNDLE_INDEX_NAME",
    "OKF_VERSION",
    "bundle_members",
    "export_bundle",
    "format_bundle_index",
]
