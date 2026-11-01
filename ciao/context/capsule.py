"""One small, provider-neutral context capsule for normal chat turns.

The capsule is deliberately separate from provider system prompts. It carries
only request-scoped routing facts; native ``CLAUDE.md`` /
``AGENTS.md`` loaders remain the source of instructions and memory.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
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

#: Code-work rules for a chat whose project is linked to a git repository.
#: Sent with the stable block (once per provider session), only when linked.
LINKED_REPO_RULES = (
    "Code-work rules for this linked repository:\n"
    "- The repository's instructions file named below governs code conventions, tests and git there; follow it.\n"
    "- Your working directory is the Ciaobot workspace, not the repository. Run repository commands inside it "
    "(`cd <path> && ...` or `git -C <path> ...`).\n"
    "- This is a shared checkout. Run `git status` before changing anything and leave changes you did not make alone. "
    "Commit or push only when the user asks or the repository's instructions say so.\n"
    "- A repository-specific learning (convention, test command, trap) goes into the repository's instructions file, "
    "in that file's style; say in your reply that you changed it.\n"
    "- Project decisions and status go in the project note (canonical_doc), not the repository.\n"
    "- Never write vault content into the repository, and never put code details into the vault.\n"
    "- End a turn that changed code with the files you changed and the branch they are on.\n"
    "- Only code and files the repository owns (including its AGENTS.md) go in the repository. Notes, plans, "
    "reports, drafts and artifacts go in the project's Ciaobot folder (canonical_doc's folder), as usual."
)

#: Sent instead of the full block when the project is linked but the chat's code mode is off.
LINKED_REPO_OFF_HINT = (
    "Code mode is off: treat this as a normal chat. When the user wants to work on the code "
    "in this repository, run `ciao chat update --code on` first."
)


def render_linked_repo_off_block(*, path: str) -> str:
    """The light ``[Linked repository]`` block of a linked chat with code mode off."""
    shown = _field(path, limit=300)
    return f"[Linked repository]\npath={shown}\n{LINKED_REPO_OFF_HINT}\n[End linked repository]"


def render_linked_repo_block(
    *,
    path: str,
    missing: bool,
    instructions_name: str,
    instructions_text: str,
    inline_instructions: bool = True,
) -> str:
    """The multi-line ``[Linked repository]`` block of the stable facts.

    The instructions text is not passed through ``_field``: it would flatten the
    file's newlines and cap it at 1200 characters. ``inline_instructions=False``
    names the file instead of quoting it: the stable block is being re-sent
    inside a provider session that already started (code mode switched on, the
    file edited, the repo linked mid-chat), and repeating up to
    ``REPO_INSTRUCTIONS_MAX_CHARS`` of text on such a turn is the cost the
    once-per-session stable block exists to avoid.
    """
    shown = _field(path, limit=300)
    if missing:
        return (
            f"[Linked repository]\npath={shown}\n"
            "The linked repository is not available on this machine (moved, deleted or no longer a git checkout). "
            "Tell the user before doing any code work there; do not recreate it.\n"
            "[End linked repository]"
        )
    block = f"[Linked repository]\npath={shown}\n{LINKED_REPO_RULES}\n"
    if instructions_name and not inline_instructions:
        block += (
            f"Repository instructions: read {shown}/{instructions_name} before code work "
            "(again if it changed since you last read it)."
        )
    elif instructions_name:
        block += (
            f"[Repository instructions: {shown}/{instructions_name}]\n{instructions_text}\n"
            "[End repository instructions]"
        )
    else:
        block += "The repository has no AGENTS.md or CLAUDE.md. Ask the user before creating an AGENTS.md; never create a CLAUDE.md."
    return block + "\n[End linked repository]"


PERMISSION_MODE_HINTS = {
    "normal": (
        "tool calls wait for the user's approval (every call on OpenCode, edits and "
        "shell on Claude), so make few, deliberate calls"
    ),
    "plan": "read-only: propose a plan and change nothing",
    "auto": "routine actions run without asking; risky ones ask the user",
    "bypass": "every action runs without asking",
}


def _file_access_line(file_roots: Sequence[str], file_read_only: Sequence[str] = ()) -> str:
    """One line: where a workspace-scoped chat may write, what it only reads, and who widens it."""
    roots = ", ".join(_field(root, limit=300) for root in file_roots)
    read_only = "".join(f"read only: {_field(path, limit=300)}; " for path in file_read_only)
    return (
        f"file_access=workspace only: read and write under {roots}; {read_only}other paths "
        'are refused by the sandbox ("Operation not permitted"). The owner changes '
        "this in Settings → Overview → File access."
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
    file_roots: Sequence[str] = (),
    file_read_only: Sequence[str] = (),
    linked_repo_block: str = "",
    linked_repo_line: str = "",
    permission_mode: str = "",
) -> str:
    """Render the compact context visible to every provider.

    ``workspace_vault_root`` is the active workspace's vault, relative to the
    provider's cwd, so a path the model writes is usable verbatim.

    ``file_roots`` are the folders a workspace-scoped chat may read and write.
    Empty means no file-access line (machine scope). ``file_read_only`` are single
    files it may read and never write (a memory pass's source archive, #2459).

    ``permission_mode`` is the chat's effective permission mode; it is sent every
    turn with a one-line hint (see ``PERMISSION_MODE_HINTS``), since it can change
    mid-session. Empty means no line.

    Stable project facts can be omitted after the first turn of a provider
    session. The date, the unattended marker, the file-access line and handover
    data are sent on every turn.

    The task-board hint is neither: it is a constant line rather than a routing
    fact, so it rides with the stable block and costs the same in every
    workspace. See ``TASK_BOARD_HINT``.

    ``linked_repo_block`` is the multi-line block of a project linked to a git
    repository (``render_linked_repo_block``); it rides the stable block.
    ``linked_repo_line`` is the short ``linked_repo=<path> branch=<branch>`` line
    sent every turn, because the branch changes too often for the stable block.
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
    if linked_repo_block:
        stable.append(linked_repo_block)

    dynamic: list[str] = [f"today={datetime.now(UTC).date().isoformat()}"]
    if unattended:
        dynamic.append(UNATTENDED_CAPSULE_GUIDANCE)
    if file_roots:
        dynamic.append(_file_access_line(file_roots, file_read_only))
    if permission_mode:
        line = f"permission_mode={_field(permission_mode, limit=20)}"
        hint = PERMISSION_MODE_HINTS.get(permission_mode)
        dynamic.append(f"{line}: {hint}" if hint else line)
    if linked_repo_line:
        dynamic.append(_field(linked_repo_line, limit=400))

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
    linked_repo: str = "",
) -> str:
    """Return a stable digest for deciding whether routing facts changed.

    ``linked_repo`` is hashed in only when non-empty, so an unlinked chat keeps
    the digest it had before linking existed.
    """
    parts = [workspace, gws_profile, project_name, project_context, canonical_doc]
    if linked_repo:
        parts.append(linked_repo)
    raw = "\0".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
