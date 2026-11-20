"""The chat notice for background shells that finished, built from the shared run-notice format.

One wake turn may cover several shells that ended within a short window. The text is the
``format_finished_runs`` notice the PWA already draws as a background-run card, so the parser and
the formatter stay one format. The output tails are untrusted: ``format_finished_runs`` fences
them, and the reader already removed control characters and ANSI.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from ciao.background_notice import format_finished_runs

if TYPE_CHECKING:
    from ciao.models import ShellFinishedEvent

#: The command is shown up to this many characters.
COMMAND_CHARS = 200
#: Output lines shown per shell (the last ones).
TAIL_LINES = 20
# Leading ``NAME=value`` assignments (and ``export``) carry environment values, which never go in the notice.
_ENV_PREFIX_RE = re.compile(r"^(?:export\s+)?(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def _label(event: ShellFinishedEvent) -> str:
    # One line: a multi-line command would break the notice's header line.
    command = " ".join(event.command.split())
    command = _ENV_PREFIX_RE.sub("", command, count=1)
    if len(command) > COMMAND_CHARS:
        command = command[: COMMAND_CHARS - 1] + "…"
    return f"{command or 'shell'} (ran {round(event.duration_s)} s)"


def shell_finished_prompt(events: list[ShellFinishedEvent]) -> str:
    """The wake prompt for *events*: one header and one block per shell."""
    entries: list[dict[str, Any]] = [
        {
            "label": _label(event),
            "run_id": event.tool_use_id,
            "status": "unknown",
            "exit_code": None,
            "error": "",
            "log_path": event.output_path,
            # None: the output could not be read, so the notice claims nothing about it.
            "last_lines": event.output_tail,
        }
        for event in events
    ]
    text, _ = format_finished_runs(entries, tail_lines=TAIL_LINES)
    return text
