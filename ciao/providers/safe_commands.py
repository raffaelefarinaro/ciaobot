"""Conservative classifier for read-only shell commands.

Used where a provider must decide, per permission request, whether a shell
command can run without the operator's approval (opencode's auto mode — see
``OpencodeProvider._permission_event``). The bias is firmly toward "not
safe": anything multi-line, carrying a construct that could write or execute
(redirection, command substitution, subshells, background jobs), or simply
unrecognized surfaces an approval card instead of being approved.
"""

from __future__ import annotations

import shlex

# Commands that only ever read, regardless of arguments. Near-misses whose
# ordinary flags write files are deliberately absent: `sort -o`/`uniq in out`
# write positional outputs, `sed -i` edits in place, `awk`/`tee`/`xargs`
# execute or write.
_SAFE_COMMANDS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "which", "echo", "printf",
    "rg", "grep", "egrep", "fgrep",
    "file", "stat", "du", "df", "tree",
    "basename", "dirname", "realpath", "readlink",
    "whoami", "hostname", "uname", "id", "date", "uptime",
    "type", "true", "false", "jq", "cut", "tr", "nl",
})

# git subcommands that never mutate the repository. `branch` is handled apart
# because it only *lists* under certain flags.
_SAFE_GIT_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "rev-parse", "ls-files",
})

# `git branch` lists only when every argument is one of these; any positional
# argument creates or renames, and an unrecognized flag is not verified.
_SAFE_GIT_BRANCH_FLAGS = frozenset({
    "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
    "-l", "--list", "--show-current", "--merged", "--no-merged",
})

# find actions that delete, execute, or write files. Prefix-matched so that
# `-execdir`/`-okdir` and the `-fprint` family are covered too.
_UNSAFE_FIND_PREFIXES = ("-delete", "-exec", "-ok", "-fprint", "-fls")

# rg flags that execute an external command (`--pre <cmd>` runs it per file)
# or a helper binary. Prefix-matched to cover the `=`-joined form too.
_UNSAFE_RG_PREFIXES = ("--pre", "--hostname-bin")

# `tree -o <file>` / `--output` writes its listing to a file.
_UNSAFE_TREE_PREFIXES = ("-o", "--output")

# Shell operators allowed between otherwise-safe segments. Everything else
# shlex emits as punctuation (`>`, `>>`, `<`, `&`, `(`, `)`) is rejected.
_CONNECTORS = frozenset({";", "&&", "||", "|"})
_PUNCTUATION = frozenset("();<>|&")


def _tokens(command: str) -> list[str] | None:
    """Shell-split with operators as their own tokens, or None if unparsable.

    ``punctuation_chars`` makes unquoted operators come out as pure
    punctuation runs while quoted ones stay inside their word — which is what
    lets ``grep "a|b"`` pass and ``ls|rm`` fail on the ``rm``.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    # shlex defaults to shell-style `#` comments, but bash only honors `#` at
    # the start of a word: `cat foo#>out.txt` truncates out.txt while shlex
    # would silently drop everything from the `#` on and classify the visible
    # prefix. Never strip; let a mid-word `#` reach the safety checks.
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quoting; whatever the shell would make of it, not this.
        return None


def _git_is_safe(args: list[str]) -> bool:
    if not args:
        return False
    subcommand, *rest = args
    if subcommand == "branch":
        return all(arg in _SAFE_GIT_BRANCH_FLAGS for arg in rest)
    if subcommand not in _SAFE_GIT_SUBCOMMANDS:
        return False
    # `--output` writes a file; `--ext-diff` and `--textconv` can invoke
    # repository-configured external helpers even on read-only subcommands.
    unsafe_prefixes = ("--output", "--ext-diff", "--textconv")
    return not any(arg.startswith(unsafe_prefixes) for arg in rest)


def _segment_is_safe(tokens: list[str]) -> bool:
    """One pipeline/connector segment: a command name and its arguments."""
    name, *args = tokens
    if name == "git":
        return _git_is_safe(args)
    if name == "find":
        return not any(arg.startswith(_UNSAFE_FIND_PREFIXES) for arg in args)
    if name == "rg":
        return not any(arg.startswith(_UNSAFE_RG_PREFIXES) for arg in args)
    if name == "tree":
        return not any(arg.startswith(_UNSAFE_TREE_PREFIXES) for arg in args)
    return name in _SAFE_COMMANDS


def is_read_only_command(command: str) -> bool:
    """True only when *command* is verifiably read-only.

    A pipeline or `;`/`&&`/`||` chain is safe only when every segment is
    independently safe, so `git status && git push` and `ls; rm -rf /` both
    fail on their second half.
    """
    text = (command or "").strip()
    if not text:
        return False
    # Rejected on the raw string, quoted or not: token-level checks cannot
    # tell a quoted backquote from a live one reliably, and a conservative
    # false positive only costs an approval card.
    if any(marker in text for marker in ("\n", "\r", "`", "$(")):
        return False
    tokens = _tokens(text)
    if not tokens:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _CONNECTORS:
            segments.append([])
        elif all(char in _PUNCTUATION for char in token):
            # Some other operator: redirection, background `&`, a subshell.
            return False
        else:
            segments[-1].append(token)
    filled = [segment for segment in segments if segment]
    return bool(filled) and all(_segment_is_safe(s) for s in filled)
