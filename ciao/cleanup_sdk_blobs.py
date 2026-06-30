"""Reclaim disk by deleting SDK session JSONL blobs for already-archived chats.

Scans ``memory-vault/Logs/Chats/**/*.md`` for session UUIDs embedded in the
filename, then removes matching ``~/.claude/projects/-<slug>/<uuid>.jsonl``
blobs. Dry-run by default; pass ``--apply`` to actually delete.

Only blobs whose UUID appears in an archived markdown filename are removed.
Anything else (including the SDK blob for live, non-archived chats) is left
alone.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def claude_projects_dir(workspace_root: Path) -> Path:
    slug = str(workspace_root.resolve()).replace("/", "-").lstrip("-")
    return Path.home() / ".claude" / "projects" / f"-{slug}"


def archived_session_ids(vault_chats_root: Path) -> set[str]:
    ids: set[str] = set()
    if not vault_chats_root.exists():
        return ids
    for md in vault_chats_root.rglob("*.md"):
        m = UUID_RE.search(md.name)
        if m:
            ids.add(m.group(0))
    return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="Workspace root (defaults to current directory)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete blobs (default is dry-run)",
    )
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    vault_chats = workspace / "memory-vault" / "Logs" / "Chats"
    projects_dir = claude_projects_dir(workspace)

    print(f"Workspace:       {workspace}")
    print(f"Vault chats:     {vault_chats}")
    print(f"SDK projects:    {projects_dir}")
    print()

    archived = archived_session_ids(vault_chats)
    print(f"Archived session IDs found: {len(archived)}")

    if not projects_dir.exists():
        print(f"No SDK projects dir at {projects_dir}; nothing to do.")
        return 0

    blobs = sorted(projects_dir.glob("*.jsonl"))
    print(f"SDK blobs present:         {len(blobs)}")

    targets = [b for b in blobs if b.stem in archived]
    total_bytes = sum(b.stat().st_size for b in targets if b.exists())
    print(
        f"Matches (archived + blob):  {len(targets)}  "
        f"({total_bytes / 1_048_576:.2f} MB)"
    )
    print()

    if not targets:
        print("Nothing to delete.")
        return 0

    for blob in targets:
        size_mb = blob.stat().st_size / 1_048_576
        action = "DELETE" if args.apply else "would delete"
        print(f"  {action}: {blob.name}  ({size_mb:.2f} MB)")
        if args.apply:
            try:
                blob.unlink()
            except OSError as exc:
                print(f"    ! failed: {exc}")

    print()
    if args.apply:
        print(f"Done. Reclaimed {total_bytes / 1_048_576:.2f} MB.")
    else:
        print("Dry-run. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
