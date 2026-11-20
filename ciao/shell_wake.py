"""The chat notice for background shells that finished, built from the shared run-notice format.

One wake turn may cover several shells that ended within a short window. The text is the
``format_finished_runs`` notice the PWA already draws as a background-run card, so the parser and
the formatter stay one format. The output tails are untrusted: ``format_finished_runs`` fences
them, and the reader already removed control characters and ANSI.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING, Any

from ciao.background_notice import format_finished_runs
from ciao.redact import redact_text

if TYPE_CHECKING:
    from ciao.models import ShellFinishedEvent

#: The command is shown up to this many characters.
COMMAND_CHARS = 200
#: Output lines shown per shell (the last ones).
TAIL_LINES = 20

_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
# A token that is a redirection operator: it and the word after it are not shown.
_REDIRECTS = frozenset({">", ">>", "<", "<<", "2>", "2>>", "&>"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A flag or header name whose value is a secret: it ends in a secret kind (``--api-key``, ``X-Api-Key``,
# ``--token``), or it is ``Authorization``. ``--author`` is not one.
_SECRET_NAME = re.compile(r"(?:^|[-_])(?:api[-_]?key|key|token|auth|secret|password|passwd|credential)s?$")


def _is_secret_name(name: str) -> bool:
    lowered = name.lstrip("-").lower()
    return lowered == "authorization" or _SECRET_NAME.search(lowered) is not None
_MASK = "[redacted]"


def _lex(command: str) -> list[str]:
    """The command's words and operators, quotes removed (``shlex``, operators kept as tokens)."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _mask_words(words: list[str]) -> list[str]:
    """One simple command's words with every secret value masked (flags, ``-u``, ``-p``, headers)."""
    out: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        following = words[index + 1] if index + 1 < len(words) else None
        if word in ("-u", "--user") and following is not None and ":" in following:
            out += [word, _MASK]  # `-u user:password`
            index += 2
            continue
        if re.match(r"^-p.", word):
            out.append(f"-p{_MASK}")  # mysql-style `-psecret`
        elif word == "-H" and following is not None:
            out += [word, _mask_header(following)]  # `-H "X-Api-Key: abc"`
            index += 2
            continue
        elif word.startswith("-H") and ":" in word:
            out.append("-H" + _mask_header(word[2:]))
        elif word.startswith("--") and "=" in word:
            name, _, _value = word.partition("=")
            out.append(f"{name}={_MASK}" if _is_secret_name(name) else word)
        elif word.startswith("--") and _is_secret_name(word) and following is not None and not following.startswith("-"):
            out += [word, _MASK]  # `--token ABC`
            index += 2
            continue
        else:
            out.append(word)
        index += 1
    return out


def _mask_header(header: str) -> str:
    """``Name: value`` with the value masked when the header name is a secret kind; else unchanged."""
    name, sep, _value = header.partition(":")
    if sep and _is_secret_name(name):
        return f"{name}: {_MASK}"
    return header


def _show(word: str) -> str:
    return f'"{word}"' if " " in word else word


def _safe_command(command: str) -> str:
    """The command as it may be shown: environment assignments and redirections dropped, secrets masked.

    A command that cannot be parsed shows only its program name.
    """
    try:
        tokens = _lex(command)
    except ValueError:
        # An unclosed quote: only the text before the first quote is trusted to name the program.
        return _program_name(re.split(r"[\"']", command, maxsplit=1)[0])
    segments: list[tuple[str, list[str]]] = [("", [])]
    skip_target = False
    for token in tokens:
        if skip_target:
            skip_target = False
            continue
        if token in _REDIRECTS:
            skip_target = True
            continue
        if token in _SEPARATORS:
            segments.append((token, []))
            continue
        segments[-1][1].append(token)
    out = ""
    for operator, words in segments:
        # Leading `export` and `NAME=value` words set environment: their values never show.
        while words and (words[0] == "export" or _ASSIGNMENT.match(words[0])):
            words = words[1:]
        if not words:
            continue
        shown = " ".join(_show(word) for word in _mask_words(words))
        out = f"{out} {operator} {shown}" if out else shown
    return out


def _program_name(command: str) -> str:
    """The first word that is not an assignment, cut to a plain program name; ``shell`` when none."""
    for word in command.split():
        if word in _SEPARATORS or word == "export" or _ASSIGNMENT.match(word):
            continue
        name = re.sub(r"[^A-Za-z0-9_./+-]", "", word)[:60]
        return name or "shell"
    return "shell"


def _label(event: ShellFinishedEvent) -> str:
    # One line: a multi-line command would break the notice's header line.
    command = " ".join(event.command.split())
    command = redact_text(_safe_command(command))
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
            "log_path": event.output_path or None,
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
