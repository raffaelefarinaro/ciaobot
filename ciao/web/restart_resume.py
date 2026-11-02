"""Resume chats whose turn an engine restart cut off (#2128).

A turn stamps ``ChatInfo.turn_owner`` with this process's token when it
starts, and the drive loop clears it when the turn ends, except during
shutdown (``ProjectChatManager.begin_shutdown``). A stamp written by any other
process is a turn whose end nobody recorded: a restart, an update or a crash
cut it off. Same derivation as ``task_attempts`` reading ``interrupted`` off
its owner token.
"""

from __future__ import annotations

import os
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciao.web.project_chats import ChatInfo

#: This process's token. A recycled pid alone could make an old stamp look
#: like ours, so the random half is what makes it unique per boot.
PROCESS_TOKEN = f"{os.getpid()}-{uuid.uuid4().hex}"
RESTART_RESUME_ORIGIN = "restart_resume"
RESTART_RESUME_PROMPT = (
    "Continue after restart: the Ciaobot engine restarted while your previous "
    "turn was still running, so it stopped partway. Pick up where you left off. "
    "Check the current state of anything you were changing (files, commands, "
    "background work) before repeating a step."
)
#: Wait after boot before resuming, like the startup wakes do.
RESUME_DELAY_SECONDS = 5.0
# Helper chats the engine runs itself; a delegated task is resumed from the board.
_NO_RESUME_HELPER_KINDS = frozenset({"memory_pass", "update_task", "proposal", "task_delegation"})
# A resume that was itself cut off is not resumed again (no crash loop); a
# delegated task's turn belongs to its attempt, which reads ``interrupted``.
_NO_RESUME_ORIGINS = frozenset({RESTART_RESUME_ORIGIN, "task_delegation", "task_resume"})


def interrupted(chat: ChatInfo) -> bool:
    """Whether *chat*'s last turn was in flight in another process."""
    return bool(chat.turn_owner) and chat.turn_owner != PROCESS_TOKEN


def skip_reason(chat: ChatInfo) -> str:
    """Why an interrupted *chat* must not be resumed, or "" when it may be."""
    if chat.archived:
        return "archived"
    helper = chat.helper if isinstance(chat.helper, dict) else {}
    if helper.get("kind") in _NO_RESUME_HELPER_KINDS:
        return "helper chat"
    if chat.pending_question or chat.pending_permission:
        return "waiting on the user"
    if chat.retry_status == "pending":
        return "retry pending"
    last = str(max(0, int(chat.user_turn_count) - 1))
    if chat.user_turn_unattended.get(last):
        return "unattended turn"
    if str(chat.user_turn_origin.get(last) or "") in _NO_RESUME_ORIGINS:
        return "engine turn"
    return ""
