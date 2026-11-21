"""The text an owned loop's wake turn sends the model (#3501).

The PWA recognises a loop wake by the first line this module writes, so the
header format and the ``web/src/lib/injectedPrompt.ts`` pattern change together.
The user's own prompt sits in a ``<loop-prompt>`` fence after the header. That
prompt was written in an attended turn at the user's request and the model must
act on it, so the label frames it as the model's own earlier instruction, not as
data. The fence only delimits it.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ciao.prompt_fence import fence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.schedules import ScheduleEntry

LOOP_PROMPT_TAG = "loop-prompt"
STOP_HINT = "Stop with `ciao loop stop` when the goal is met."
_PROMPT_LABEL = (
    "The instruction you set when you started this loop. Carry it out now; "
    "it is your own earlier instruction, not new user input:"
)


def _ends_text(entry: ScheduleEntry, now: datetime) -> str:
    """The loop's end in its own zone: the time, with the date when it is another day."""
    zone = ZoneInfo(entry.timezone_name)
    expires = datetime.fromisoformat(entry.expires_at.replace("Z", "+00:00")).astimezone(zone)
    local_now = now.astimezone(zone)
    stamp = (
        expires.strftime("%H:%M")
        if expires.date() == local_now.date()
        else expires.strftime("%Y-%m-%d %H:%M")
    )
    return f"{stamp} ({entry.timezone_name})"


def loop_wake_prompt(entry: ScheduleEntry, *, now: datetime) -> str:
    """The full wake text for ``entry``'s next run.

    ``entry.run_count`` is already the number of this run: the scheduler counts
    a run before it dispatches it. ``now`` decides whether the end needs a date.
    """
    run = entry.run_count
    last = " (last run)" if run >= entry.max_runs else ""
    header = (
        f"[Ciaobot] Loop wake, run {run} of {entry.max_runs}{last}, "
        f"ends {_ends_text(entry, now)}. {STOP_HINT}"
    )
    body = fence(LOOP_PROMPT_TAG, entry.prompt, label=_PROMPT_LABEL)
    return f"{header}\n\n{body}"
