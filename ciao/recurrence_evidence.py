"""Read-only lookup of persisted task completions for the recurrence gate (#3238-B).

:func:`ciao.recurrence.classify_recurrence` decides from resolved evidence; this
module produces that evidence from the workspace's task files. It writes
nothing, takes no lock and never touches the curation state. Callers run it
before they take any lock of their own, because it reads every task file in the
workspace (bounded) and a queue lock should not be held across that read.

A completion counts only when a task file on disk holds it. An id nobody
persisted resolves to ``None``, so an invented id is never a supported outcome.
A task file or decisions file this code cannot read raises
:class:`EvidenceUnavailable` rather than resolving to absent: a completion that
might sit in an unreadable file must never be read as "not there".
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ciao.recurrence import Outcome
from ciao.task_board import TASKS_RELATIVE, TaskBoardError, TaskBoardStore
from ciao.task_decisions import read_decisions_strict
from ciao.task_resolution import parse_completions

if TYPE_CHECKING:
    from ciao.config import CiaoConfig

#: How many completion ids one lookup resolves. A claim longer than this is
#: resolved only up to the bound; the rest stay unresolved, which the classifier
#: reads as unsupported. Sized well above the three outcomes the gate needs.
MAX_IDS = 16

#: How many task files one lookup will read. A workspace whose task directory is
#: larger than this is not scanned in part: that would be a partial read dressed
#: as a complete one, so the lookup is unavailable instead.
MAX_TASK_FILES = 2000


class EvidenceUnavailable(Exception):
    """The persisted history cannot be read in full, so no completion can be vouched for."""


def resolve_completions(
    config: CiaoConfig, workspace: str, ids: Sequence[str]
) -> dict[str, Outcome | None]:
    """Map each asked-for completion id to the task that persisted it, or ``None``.

    At most :data:`MAX_IDS` distinct ids are looked up, in first-appearance order.
    Non-string and blank entries are skipped. Raises :class:`EvidenceUnavailable`
    when a task file or the decisions file is unreadable or malformed, or when the
    task directory holds more than :data:`MAX_TASK_FILES` files.
    """
    wanted: list[str] = []
    for item in ids:
        if isinstance(item, str) and item.strip() and item not in wanted:
            wanted.append(item)
        if len(wanted) == MAX_IDS:
            break
    resolved: dict[str, Outcome | None] = dict.fromkeys(wanted)
    if not wanted:
        return resolved

    vault = Path(config.workspace_vault_root(workspace))
    decisions = read_decisions_strict(vault)
    if decisions.status not in ("ok", "absent"):
        raise EvidenceUnavailable(
            f"the decisions file is {decisions.status}, so the task history cannot be "
            "read in full"
        )
    tasks_dir = vault / TASKS_RELATIVE
    if not tasks_dir.is_dir():
        return resolved
    names = [entry for entry in tasks_dir.iterdir() if entry.suffix == ".md"]
    if len(names) > MAX_TASK_FILES:
        raise EvidenceUnavailable(
            f"the workspace holds more than {MAX_TASK_FILES} task files, which is more "
            "than one lookup reads"
        )

    store = TaskBoardStore(
        workspace=workspace,
        vault_root=vault,
        runtime_dir=Path(config.state_path).parent,
        clock=lambda: datetime.now(UTC),
    )
    try:
        listing = store.list()
    except (TaskBoardError, OSError) as exc:
        raise EvidenceUnavailable(f"the task board cannot be read: {exc}") from None
    if listing.invalid:
        raise EvidenceUnavailable(
            f"{len(listing.invalid)} task file(s) are unreadable, so a completion "
            "cannot be ruled out of them"
        )
    for document in listing.tasks:
        for completion in parse_completions(document.body):
            if completion.id in resolved and resolved[completion.id] is None:
                resolved[completion.id] = Outcome(
                    completion_id=completion.id, task_id=document.record.id
                )
    return resolved
