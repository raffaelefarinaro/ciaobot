"""Observational preparation of update tasks; never authorizes an apply."""

import logging
import re
from pathlib import Path
from typing import Any

from ciao import learnings_cleanup, learnings_queue_drain, vault_rehome, vault_relocate
from ciao.update_task_catalog import UpdateTask

logger = logging.getLogger(__name__)
_GENERIC = "Could not prepare this task; no changes were made."
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.-])(?:[A-Za-z]:[\\/]|/(?!/)|\\\\|~/)[^\s\"'<>;,)]*")


def _redact_drain_paths(value: Any, roots: tuple[str, ...]) -> Any:
    """Remove host paths recursively from diagnostic plan data."""
    if isinstance(value, str):
        redacted = value
        for root in roots:
            if root:
                redacted = redacted.replace(root, "<workspace path>")
        return _ABSOLUTE_PATH_RE.sub("<path>", redacted)
    if isinstance(value, list):
        return [_redact_drain_paths(item, roots) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_drain_paths(item, roots) for item in value)
    if isinstance(value, dict):
        return {key: _redact_drain_paths(item, roots) for key, item in value.items()}
    return value


def _reviewable_drain_plan(
    plan: dict[str, Any], *, vault: Path, runtime_root: Path
) -> dict[str, Any]:
    """Keep receipt basenames while stripping host paths from the preview."""
    public = dict(plan)
    for key in ("receipts", "applied_receipts"):
        paths = public.get(key)
        if isinstance(paths, list):
            public[key] = [Path(str(path)).name for path in paths]
    roots = (str(vault.resolve()), str(runtime_root.resolve()))
    redacted_plan: dict[str, Any] = _redact_drain_paths(public, roots)
    return redacted_plan


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
        if task.id == "learnings-queue-drain":
            vault = Path(config.workspace_vault_root(workspace))
            plan = learnings_queue_drain.drain(
                vault, workspace=workspace, runtime_root=Path(config.state_path).parent
            )
            rows = plan["rows"]
            blocked = bool(plan["failed"] or plan["unresolved"])
            plan = _reviewable_drain_plan(
                plan, vault=vault, runtime_root=Path(config.state_path).parent
            )
            result.update(
                state="blocked" if blocked else "review" if rows else "not_applicable",
                plan=plan,
                summary=(
                    f"Blocked: {len(plan['unresolved'])} drain transaction(s) need recovery or inspection."
                    if blocked
                    else f"{len(rows)} pending learning row(s) are ready to drain. Review and apply the receipt-backed command."
                ),
            )
        elif task.id == "learnings-cleanup":
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
