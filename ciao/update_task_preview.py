"""Observational preparation of update tasks; never authorizes an apply."""

import logging
from pathlib import Path
from typing import Any

from ciao import learnings_cleanup, vault_rehome, vault_relocate
from ciao.update_task_catalog import UpdateTask

logger = logging.getLogger(__name__)
_GENERIC = "Could not prepare this task; no changes were made."


def prepare(task: UpdateTask, *, config: Any, workspace: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_id": task.id,
        "revision": task.revision,
        "workspace": workspace,
        "state": "unknown",
        "summary": _GENERIC,
        "plan": None,
    }
    try:
        names = list(config.workspace_names())
    except Exception:
        logger.exception("Could not read workspace registry for task preview")
        return result
    if not workspace or workspace not in names:
        raise ValueError("workspace must be registered")
    try:
        if task.id == "learnings-cleanup":
            plan = learnings_cleanup.plan_cleanup(
                Path(config.workspace_vault_root(workspace)),
                workspace=workspace,
                config=config,
            ).as_dict()
            counts = plan["counts"]
            summary = (
                f"{counts['active']} active, {counts['remove']} removals, "
                f"{counts['keep']} kept, {counts['conflict']} conflicts. Review required."
            )
            if plan["blocked"]:
                summary = f"Blocked: {plan['blocked']}. {summary}"
            result.update(
                state="blocked" if plan["blocked"] else "review",
                plan=plan,
                summary=summary,
            )
        elif task.id == "vault-relocate":
            plan = vault_relocate.plan(config, workspace, workspaces=names).as_dict()
            if plan["refused"]:
                reasons = list(plan["refusals"])
                reasons.extend(
                    f"{entry['name']}: {entry['reason']}"
                    for entry in plan["entries"]
                    if entry["action"] == "unclassified"
                )
                summary = f"Blocked: {'; '.join(reasons)}."
            else:
                moves = sum(entry["action"] == "move" for entry in plan["entries"])
                movement = "the whole directory" if plan["whole_directory"] else f"{moves} entries"
                summary = (
                    f"The planner proposes moving {movement}. "
                    "Separate approval and restart required."
                )
            result.update(
                state="blocked" if plan["refused"] else "review",
                plan=plan,
                summary=summary,
            )
        elif task.id == "unrehomed-people":
            raw = vault_rehome.plan_rehome(Path(config.vault_root), workspaces=names)
            keys = (
                "workspaces",
                "mechanical",
                "needs_judgement",
                "moves",
                "conflicts",
                "notes_scanned",
                "read_errors",
                "skipped",
            )
            plan = {key: raw[key] for key in keys if key in raw}
            blocked = bool(raw.get("skipped") or raw["conflicts"] or raw["read_errors"])
            summary = (
                f"The planner proposes {len(raw['moves'])} moves; "
                f"{len(raw['needs_judgement'])} cases need judgment, "
                f"{len(raw['conflicts'])} conflicts, {len(raw['read_errors'])} unreadable files. "
                "Review required."
            )
            if blocked:
                reason = f" {raw['skipped']}." if raw.get("skipped") else ""
                summary = f"Blocked:{reason} {summary}"
            result.update(state="blocked" if blocked else "review", plan=plan, summary=summary)
        else:
            result["summary"] = "This task has no automatic preview; no changes were made."
    except Exception:
        logger.exception("Could not prepare update task preview")
    return result
