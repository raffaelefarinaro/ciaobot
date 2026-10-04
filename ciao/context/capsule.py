"""One small, provider-neutral context capsule for normal chat turns.

The capsule is deliberately separate from provider system prompts. It carries
only request-scoped routing facts; native ``CLAUDE.md`` /
``AGENTS.md`` loaders remain the source of instructions and memory.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from ciao.memory_policy import UNATTENDED_CAPSULE_GUIDANCE


def _field(value: str, *, limit: int = 1200) -> str:
    """Keep user-controlled routing metadata single-line and bounded."""
    return " ".join(str(value or "").split())[:limit]


#: The one bounded line that tells an agent the task board exists.
#:
#: A recipe, never the board. #973's plan is explicit that an agent fetches "the
#: relevant tasks on request or during a delegated task, not the entire board on
#: every turn", and a task record is the user's own Markdown: pushing titles,
#: dates and bodies into a capsule every provider sees would spend context on
#: work nobody asked for and put bookkeeping prose into the one surface #1002
#: keeps out of recall. So this names where the board is read from and stops.
#:
#: It is a constant, which is why it carries no ``_field`` limit of its own and
#: why ``context_digest`` does not see it: routing facts decide whether the
#: stable block is re-sent, and this line is the same in every workspace of every
#: install. It rides with the stable facts because it is stable, so a provider
#: session reads it on its first turn and not again.
TASK_BOARD_HINT = (
    'task_board="ciao task list | ciao task get TASK_ID | ciao task delegate '
    'TASK_ID --revision REV" — this workspace\'s task board, one Markdown record '
    'per task. Fetch the tasks this turn needs, or the one you were delegated; '
    'never the whole board. Delegating hands a task to the agent as one ordinary '
    'attended chat, and a finished turn waits for the user — only they mark a '
    'task done.'
)


def build_context_capsule(
    *,
    workspace: str = "",
    gws_profile: str = "",
    project_name: str = "",
    project_context: str = "",
    canonical_doc: str = "",
    workspace_vault_root: str = "",
    unattended: bool = False,
    handover: str = "",
    include_stable: bool = True,
) -> str:
    """Render the compact context visible to every provider.

    ``workspace_vault_root`` is the active workspace's vault, relative to the
    provider's cwd, so a path the model writes is usable verbatim.

    Stable project facts can be omitted after the first turn of a provider
    session. The date, the unattended marker and handover data are sent on
    every turn.

    The task-board hint is neither: it is a constant line rather than a routing
    fact, so it rides with the stable block and costs the same in every
    workspace. See ``TASK_BOARD_HINT``.
    """

    stable: list[str] = []
    if workspace:
        stable.append(f"workspace={_field(workspace, limit=120)}")
    if workspace_vault_root:
        # The workspace *name* is not a location. Without this the model knew it
        # was in "work" but had to guess where that workspace's vault lived, and
        # guessed from precedent — which on a vault whose People/ folder had been
        # filled by the old single-workspace curator meant writing every new
        # contact back into the wrong workspace. Naming the path is what stops
        # the misfiling recurring at the write step.
        stable.append(f"vault={_field(workspace_vault_root, limit=300)}")
    if gws_profile:
        stable.append(f"gws_profile={_field(gws_profile, limit=120)}")
    if project_name and project_name != "General":
        stable.append(f'project="{_field(project_name, limit=180)}"')
    if project_context:
        stable.append(f"project_context={_field(project_context)}")
    if canonical_doc:
        stable.append(f"canonical_doc={_field(canonical_doc, limit=300)}")
    stable.append(_field(TASK_BOARD_HINT, limit=400))

    dynamic: list[str] = [f"today={datetime.now(UTC).date().isoformat()}"]
    if unattended:
        dynamic.append(UNATTENDED_CAPSULE_GUIDANCE)

    parts: list[str] = []
    if include_stable:
        parts.extend(stable)
    parts.extend(dynamic)
    if handover:
        parts.append(handover)
    if not parts:
        return ""
    return "<ciao-context>\n" + "\n".join(parts) + "\n</ciao-context>"


def context_digest(
    *,
    workspace: str,
    gws_profile: str,
    project_name: str,
    project_context: str,
    canonical_doc: str,
) -> str:
    """Return a stable digest for deciding whether routing facts changed."""
    raw = "\0".join(
        (workspace, gws_profile, project_name, project_context, canonical_doc)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
