"""Refuse to upgrade a 1.10 install whose update migrations are half done.

2.0 upgrades from 1.10 only and carries none of the old migration code. 1.10
records each migration in ``<runtime>/migration/*.json``; this reads those
receipts and says which 1.10 step to finish before 2.0 takes the install over.
It reads only, imports nothing from ``ciao`` (the Linux installer runs it from
the not-yet-installed wheel with ``python -m ciao.upgrade_preflight <workspace>``)
and never raises.

A receipt that is present must be complete and readable. A receipt that is
missing is unfinished work only where the old layout proves it: a vault still
nested at ``<install>/memory-vault`` means the workspace re-rooting never ran.
The other migrations are optional or run at 1.10 boot, so their absence says
nothing; their partial or unreadable receipts do.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

REFUSAL_CODE = "unfinished_migration"

_REROOT = "workspace-rooting.json"
_LOGS = "logs-split.json"
_REHOME = "vault-rehome.json"
_LINKS = "vault-links.json"
_VOCABULARY = "vault-vocabulary*.json"
_LEARNINGS = "learnings-*.json"

# Where in 1.10 each migration is finished. 1.10 names an update task or
# operator action only for some of them; the rest run at engine start or from a
# command.
_REMEDY = {
    _REROOT: (
        "the `workspace-unmigrated` action (or `ciao workspace-reroot apply`)"
    ),
    _LOGS: "a 1.10 restart: the Logs split runs when the engine starts",
    _REHOME: "the `unrehomed-people` update task",
    _LINKS: "`ciao vault-migrate-links --apply` (or `ciao vault-unmigrate-links --apply` to undo it)",
    _VOCABULARY: "a 1.10 restart: the vocabulary migration runs when the engine starts",
    _LEARNINGS: "the `learnings-cleanup` update task",
}


@dataclass(frozen=True)
class Finding:
    receipt: str
    problem: str  # "missing" | "unreadable" | "status:<value>"

    @property
    def message(self) -> str:
        remedy = next(text for pattern, text in _REMEDY.items() if fnmatch(self.receipt, pattern))
        what = {
            "missing": "has not run (no receipt)",
            "unreadable": "left a receipt that cannot be read",
        }.get(self.problem, f"is not finished ({self.problem.removeprefix('status:')})")
        return (
            f"Finish the 1.10 update first: migration/{self.receipt} {what}. "
            f"Open 1.10 and run {remedy}, then upgrade again."
        )


def _load(path: Path) -> tuple[str, dict[str, Any]]:
    """("missing" | "unreadable" | "ok", the parsed object)."""
    try:
        if not path.is_file():
            return "missing", {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unreadable", {}
    return ("ok", data) if isinstance(data, dict) else ("unreadable", {})


def check(install_root: Path, runtime_root: Path) -> Finding | None:
    """The first unfinished 1.10 migration of this install, or None."""
    base = Path(runtime_root) / "migration"
    try:
        reroot_state, reroot = _load(base / _REROOT)
        old_layout = (Path(install_root) / "memory-vault").is_dir()
        # A receipt is the layout marker: a fresh install has no old vault dir
        # and no receipt, and is allowed.
        if reroot_state == "missing" and old_layout:
            return Finding(_REROOT, "missing")
        if reroot_state == "unreadable":
            return Finding(_REROOT, "unreadable")
        if reroot_state == "ok" and reroot.get("status") != "migrated":
            return Finding(_REROOT, f"status:{reroot.get('status')}")

        logs_state, logs = _load(base / _LOGS)
        # Installs born per-root never had a shared archive to split.
        if logs_state == "missing" and reroot_state == "ok" and not reroot.get("born_per_root"):
            return Finding(_LOGS, "missing")
        if logs_state == "unreadable":
            return Finding(_LOGS, "unreadable")
        if logs_state == "ok" and logs.get("status") != "migrated":
            return Finding(_LOGS, f"status:{logs.get('status')}")

        # Receipts written before `status` existed record a completed run.
        for name in (_REHOME, _LINKS):
            state, data = _load(base / name)
            if state == "unreadable":
                return Finding(name, "unreadable")
            if state == "ok" and data.get("status", "migrated") != "migrated":
                return Finding(name, f"status:{data.get('status')}")

        for pattern in (_VOCABULARY, _LEARNINGS):
            for path in sorted(base.glob(pattern)):
                if _load(path)[0] == "unreadable":
                    return Finding(path.name, "unreadable")
    except OSError:
        return Finding(_REROOT, "unreadable")
    return None


def main(argv: list[str]) -> int:
    """``python -m ciao.upgrade_preflight <workspace>``: 0 clear, 4 refused."""
    if len(argv) != 1:
        print("usage: python -m ciao.upgrade_preflight <workspace>", file=sys.stderr)
        return 2
    workspace = Path(argv[0])
    finding = check(workspace, workspace / ".runtime")
    if finding is None:
        return 0
    print(finding.message, file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
