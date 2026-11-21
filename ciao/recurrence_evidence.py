"""Read-only lookup of persisted task completions for the recurrence gate (#3238-B).

:func:`ciao.recurrence.classify_recurrence` decides from resolved evidence; this
module produces that evidence from the workspace's task files. It writes
nothing, takes no lock and never touches the curation state. Callers run it
before they take any lock of their own, because it reads every task file in the
workspace (bounded) and a queue lock should not be held across that read.

A completion counts only when a task file on disk holds it. An id nobody
persisted resolves to ``None``, so an invented id is never a supported outcome.
An unreadable task file (a task-named ``<32hex>.md`` the board refuses) is
reported on :attr:`Lookup.unreadable`, and the caller turns an unresolved id into
"unavailable" only when the verdict is not met: a completion that might sit in
that file must never be read as "not there", while a missing file can only lower
a count. Other files in the directory are not tasks and are ignored.

Known limit: the completions a new skill cites come from ``Workspace/Learnings.md``,
which a model writes. This lookup proves that the cited completions exist and are
distinct tasks. It does not prove they are instances of the same procedure.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ciao.recurrence import Outcome
from ciao.task_board import TASKS_RELATIVE, TaskBoardError, TaskBoardStore
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


#: A task file's name: the 32-hex id the task store mints, plus ``.md``. Anything
#: else in the task directory (a README, a backup) is not a task and is ignored.
_TASK_FILE = re.compile(r"[0-9a-f]{32}\.md")


@dataclass(frozen=True, slots=True)
class Lookup:
    """What one lookup found.

    ``outcomes`` maps each asked-for id to its :class:`Outcome` or ``None``.
    ``unreadable`` is true when a task file named like a task could not be read,
    so an id left ``None`` may sit inside it. A missing file cannot raise a count,
    so only an unreadable one can make a ``None`` mean "unknown" rather than
    "absent".
    """

    outcomes: dict[str, Outcome | None]
    unreadable: bool


def resolve_completions(
    config: CiaoConfig, workspace: str, ids: Sequence[str]
) -> Lookup:
    """Find the task that persisted each asked-for completion id.

    At most :data:`MAX_IDS` distinct ids are looked up, in first-appearance order.
    Non-string and blank entries are skipped. Raises :class:`EvidenceUnavailable`
    only when the task board cannot be listed at all, or when the workspace holds
    more than :data:`MAX_TASK_FILES` task-named files. An individual unreadable
    task file is not an exception: it is reported through ``Lookup.unreadable``,
    and the caller decides whether it matters to the verdict.
    """
    wanted: list[str] = []
    for item in ids:
        if isinstance(item, str) and item.strip() and item not in wanted:
            wanted.append(item)
        if len(wanted) == MAX_IDS:
            break
    resolved: dict[str, Outcome | None] = dict.fromkeys(wanted)
    vault = Path(config.workspace_vault_root(workspace))
    tasks_dir = vault / TASKS_RELATIVE
    if not wanted or not tasks_dir.is_dir():
        return Lookup(resolved, unreadable=False)

    task_files = [
        entry for entry in tasks_dir.iterdir() if _TASK_FILE.fullmatch(entry.name)
    ]
    if len(task_files) > MAX_TASK_FILES:
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
    unreadable = any(
        _TASK_FILE.fullmatch(Path(entry.relative_path).name) is not None
        for entry in listing.invalid
    )
    for document in listing.tasks:
        for completion in parse_completions(document.body):
            if completion.id in resolved and resolved[completion.id] is None:
                resolved[completion.id] = Outcome(
                    completion_id=completion.id, task_id=document.record.id
                )
    return Lookup(resolved, unreadable=unreadable)
