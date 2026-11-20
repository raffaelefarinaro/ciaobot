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
from ciao.redact import redact_text

if TYPE_CHECKING:
    from ciao.models import ShellFinishedEvent

#: The command is shown up to this many characters.
COMMAND_CHARS = 200
#: Output lines shown per shell (the last ones).
TAIL_LINES = 20

# A shell word that may be quoted: "…" (with escapes), '…', or a run of non-space characters.
_WORD = r"""(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^\s"']+)*"""
# A leading environment assignment, `NAME=value` (the value may be quoted and contain spaces), with
# an optional `export` in front of it.
_ASSIGNMENT_RE = re.compile(rf"^(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*={_WORD}\s*")
# A command separator: a shell list operator (`&&`, `||`, `;`, `|`) or a newline.
_SEPARATOR_RE = re.compile(r"(\s*(?:&&|\|\||;|\|)\s*)")


def _strip_assignments(segment: str) -> str:
    """*segment* with its leading ``NAME=value`` and ``export NAME=value`` assignments removed."""
    rest = segment.lstrip()
    while True:
        match = _ASSIGNMENT_RE.match(rest)
        if match is None or match.end() == 0:
            return rest
        rest = rest[match.end():]


def _without_environment(command: str) -> str:
    """The command with every environment assignment dropped, before or between its command parts.

    Environment values never reach the notice: ``API_KEY=x make``, ``export S="a b" && make`` and
    ``cd app && TOKEN='x y' npm start`` all show only the commands they run. A part left with no
    command is dropped together with the separator before it.
    """
    parts = _SEPARATOR_RE.split(command)
    commands = [_strip_assignments(part) for part in parts[0::2]]
    separators = parts[1::2]
    out = ""
    for index, text in enumerate(commands):
        if not text:
            continue
        out = text if not out else f"{out}{separators[index - 1]}{text}"
    return out


def _label(event: ShellFinishedEvent) -> str:
    # One line: a multi-line command would break the notice's header line.
    command = " ".join(event.command.split())
    command = redact_text(_without_environment(command))
    if len(command) > COMMAND_CHARS:
        command = command[: COMMAND_CHARS - 1] + "…"
    return f"{command or 'shell'} (ran {round(event.duration_s)} s)"


def _status(event: ShellFinishedEvent) -> tuple[str, int | None]:
    """The run status and exit code the notice shows: the real code when there is one, else unknown."""
    if event.exit_code is None:
        return "unknown", None
    return ("ok" if event.exit_code == 0 else "failed"), event.exit_code


def shell_finished_prompt(events: list[ShellFinishedEvent]) -> str:
    """The wake prompt for *events*: one header and one block per shell."""
    entries: list[dict[str, Any]] = []
    for event in events:
        status, exit_code = _status(event)
        entries.append({
            "label": _label(event),
            "run_id": event.tool_use_id,
            "status": status,
            "exit_code": exit_code,
            "error": "",
            "log_path": event.output_path,
            # None: the output could not be read, so the notice claims nothing about it.
            "last_lines": event.output_tail,
        })
    text, _ = format_finished_runs(entries, tail_lines=TAIL_LINES)
    return text


def paused_prompt(count: int) -> str:
    """The one capped notice sent when shell wakes pause: the same header, no run blocks."""
    return (
        f"[Ciaobot] {count} background run{'s' if count != 1 else ''} finished.\n\n"
        "Shells keep finishing; wakes paused until you reply."
    )
