"""Conservative classifiers for shell command safety.

Used where a provider must decide, per permission request, whether a shell
command can run without the operator's approval (opencode's auto mode — see
``OpencodeProvider._permission_event``).

``is_read_only_command`` answers "is this verifiably read-only?" and is
firmly biased toward "not safe": anything multi-line, carrying a construct
that could write or execute (redirection, command substitution, subshells,
background jobs), or simply unrecognized surfaces an approval prompt instead
of being approved.

``is_destructive_command`` answers "does this risk deleting, destroying, or
irreversibly changing data or system state?" — the complement used by the
permissive opencode default (allow every tool except destructive shell).
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

# Commands that only ever read, regardless of arguments. Near-misses whose
# ordinary flags write files are deliberately absent: `sort -o`/`uniq in out`
# write positional outputs, `sed -i` edits in place, `awk`/`tee`/`xargs`
# execute or write.
_SAFE_COMMANDS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "which", "echo", "printf",
    "rg", "grep", "egrep", "fgrep",
    "file", "stat", "du", "df", "tree",
    "basename", "dirname", "realpath", "readlink",
    "whoami", "uname", "id", "uptime",
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

_DATE_FLAGS = frozenset({
    "-u", "--utc", "--universal", "-R", "--rfc-email", "--debug",
    "--help", "--version",
})
_DATE_VALUE_FLAGS = frozenset({
    "-d", "--date", "-f", "--file", "-r", "--reference",
})
_HOSTNAME_FLAGS = frozenset({
    "-s", "--short", "-f", "--fqdn", "-d", "--domain", "-i",
    "--ip-address", "-I", "--all-ip-addresses", "-A", "--all-fqdns",
    "--help", "--version",
})


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


def _date_is_safe(args: list[str]) -> bool:
    """Allow display-only date forms, never clock-setting options."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("+"):
            index += 1
            continue
        if arg in _DATE_VALUE_FLAGS:
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if arg in _DATE_FLAGS or arg.startswith(("-I", "--iso-8601=", "--rfc-3339=")):
            index += 1
            continue
        return False
    return True


def _hostname_is_safe(args: list[str]) -> bool:
    """Allow hostname queries only; positional/file forms can mutate it."""
    return all(arg in _HOSTNAME_FLAGS for arg in args)


def _segment_is_safe(tokens: list[str]) -> bool:
    """One pipeline/connector segment: a command name and its arguments."""
    name, *args = tokens
    if name == "git":
        return _git_is_safe(args)
    if name == "date":
        return _date_is_safe(args)
    if name == "hostname":
        return _hostname_is_safe(args)
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


# ── Destructive-command classifier ──────────────────────────────────────
# Shell verbs that remove, truncate, or otherwise destroy data / system
# state, without a clear read-write intent that leaves the data intact. This
# is the narrow complement to ``is_read_only_command``: the permissive
# opencode default allows every tool except commands that reach one of these,
# which still surface an approval prompt.
#
# The set is deliberately small. Plain file writes (``cat > f``, ``tee``,
# ``cp``, ``mv``), package managers, and normal ``git`` use are *not* listed:
# they mutate but do not remove, and flagging them would turn the default into
# the old ask-everything behavior the operator asked to drop. Git's own
# destructive verbs (``clean``, ``reset``, ``restore`` with a target, ``rm``,
# ``push --force``) are handled in ``_git_is_destructive``.
_DESTRUCTIVE_COMMANDS = frozenset({
    # File/dir removal and truncation.
    "rm", "rmdir", "rmt", "unlink", "shred", "truncate", "wipefs",
    # Filesystem/block-device destruction.
    "mkfs", "mkfs.ext4", "mkfs.xfs", "mkswap", "fdisk", "parted",
    "dd", "blkdiscard", "pvremove", "vgremove", "lvremove",
    # System state teardown.
    "shutdown", "reboot", "poweroff", "halt", "init", "telinit",
    "userdel", "groupdel", "visudo",
})

# ``find -delete`` and ``find -exec rm`` delete without naming rm as the
# leading verb, so the same prefixes as ``_UNSAFE_FIND_PREFIXES`` apply.
_DESTRUCTIVE_GIT_SUBCOMMANDS = frozenset({"clean", "reset", "restore", "rm", "prune"})
_DESTRUCTIVE_GIT_PUSH_FLAGS = frozenset({
    "-f", "--force", "--force-with-lease",
})

# Wrappers that merely elevate or prefix; the destructive verb follows them.
# `xargs` belongs here too: `find . | xargs rm -f` never names rm as a segment
# leader, which is exactly how it slipped past.
_DESTRUCTIVE_WRAPPERS = frozenset({
    "sudo", "doas", "command", "env", "time", "timeout", "nohup", "nice",
    "ionice", "stdbuf", "setsid", "watch", "xargs",
})

# A shell re-enters this classifier through its own `-c` payload; without a
# payload it is reading a script or stdin that cannot be inspected at all,
# which is what makes `curl … | sh` the shape it is.
_SHELLS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "ash",
    "busybox", "pwsh", "powershell",
})

# Interpreters given code on the command line. The code is opaque, so an
# inline-code invocation is treated as destructive; running a *script file* is
# not, because `python manage.py` is ordinary use and flagging it would put a
# card in front of nearly every project command.
_INTERPRETERS = frozenset({
    "python", "python2", "python3", "perl", "ruby", "node", "deno", "bun",
    "php", "lua", "Rscript", "osascript", "tclsh", "gawk", "awk",
})
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-E", "-r", "--eval", "--exec", "-p", "-n"})

# Operands that mean "the program arrives on stdin", which is just as opaque as
# `-c` and was the way round the inline-code check: `python3 - <<'PY' … PY`
# carries no flag at all.
_STDIN_OPERANDS = frozenset({"-", "/dev/stdin", "/dev/fd/0"})

# Sentinel kept in a segment when input redirection was stripped as punctuation.
# The `<<` of a heredoc otherwise vanished, leaving the delimiter word looking
# like an ordinary script filename.
_REDIRECT_IN = "\x00<"

# `$(…)` and backticks carry a whole command inside another one. They are
# pulled out and classified in their own right before the outer command is
# tokenized, so `echo $(rm -rf ~/x)` cannot hide the removal mid-segment.
_SUBSTITUTION_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


def _verb(name: str) -> str:
    """The bare verb behind a possibly path-qualified name.

    `/bin/rm` and `./rm` are the same command as `rm`; classifying on the
    literal token let an absolute path through untouched.
    """
    cleaned = (name or "").strip()
    if not cleaned or "/" not in cleaned:
        return cleaned
    return PurePosixPath(cleaned).name


def _unwrap(args: list[str]) -> tuple[str, list[str]] | None:
    """The real command inside a wrapper's arguments.

    Skips the wrapper's own flags and operands - `timeout 5 rm -rf x`,
    `env FOO=1 rm -rf x`, `xargs -0 rm -f` - and returns the first token that
    can plausibly be a command, with the remainder as its arguments.
    """
    for index, arg in enumerate(args):
        if arg.startswith("-") or arg.replace(".", "", 1).isdigit() or "=" in arg:
            continue
        return arg, args[index + 1 :]
    return None

# A destructive verb run with only query flags is a help/version request, not
# a removal. Anything else (including a bare `rm file`) stays destructive.
_DESTRUCTIVE_QUERY_FLAGS = frozenset({"-h", "--help", "-V", "--version"})


# Git's global options sit BEFORE the subcommand (`git -C <path> reset --hard`),
# so `args[0]` is not the subcommand whenever one is present. These take a value;
# the rest are flags. `--opt=value` is one token and needs no lookahead.
_GIT_GLOBAL_VALUE_OPTS = frozenset({
    "-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
    "--super-prefix", "--config-env",
})


def _git_subcommand(args: list[str]) -> list[str]:
    """Drop Git's global options so the real subcommand leads the list.

    Without this, every destructive-subcommand check was answered against
    `-C`, and `git -C /tmp/repo reset --hard` classified clean.
    """
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            return args[index:]
        if token in _GIT_GLOBAL_VALUE_OPTS:
            index += 2  # the option and its separate value
            continue
        index += 1
    return []


def _git_is_destructive(args: list[str]) -> bool:
    args = _git_subcommand(args)
    if not args:
        return False
    subcommand, *rest = args
    if subcommand == "push":
        return any(a.startswith(f) for a in rest for f in _DESTRUCTIVE_GIT_PUSH_FLAGS)
    if subcommand in {"checkout", "switch"}:
        # `checkout` is two commands wearing one name. Moving between refs
        # (`git checkout main`, `-b feature`) destroys nothing, but the pathspec
        # and force forms discard every uncommitted change they touch - which is
        # exactly as unrecoverable as `restore`, already listed below. Only the
        # discarding forms are flagged, so branch work is not carded.
        if any(a in {"-f", "--force", "--discard-changes"} for a in rest):
            return True
        # `--` introduces a pathspec: `git checkout -- .` throws the worktree
        # away. A bare `.` does the same without the separator.
        return "--" in rest or "." in rest
    if subcommand in _DESTRUCTIVE_GIT_SUBCOMMANDS:
        return True
    return False


def _segment_is_destructive(name: str, args: list[str]) -> bool:
    """Whether one command segment removes, truncates, or destroys data."""
    name = _verb(name)
    if name in _DESTRUCTIVE_WRAPPERS:
        # `sudo <destructive>` still destroys; drop wrappers and re-look.
        # RECURSE, rather than giving up when the next token is also a wrapper:
        # stopping there meant `sudo env FOO=1 rm -rf /tmp/x` classified clean,
        # because `env` is a wrapper too. Wrappers nest arbitrarily
        # (`sudo nohup env … rm`), so the loop has to as well, and an
        # unresolvable chain fails CLOSED rather than being waved through.
        inner = _unwrap(args)
        if inner is None:
            # A wrapper with no command after it (`sudo -l`, a bare `env`) is a
            # query, not a removal.
            return False
        return _segment_is_destructive(inner[0], inner[1])
    if name in _SHELLS:
        # `sh -c '<code>'` is judged on the code it carries. A shell with no
        # inspectable payload - `curl … | sh`, `bash script.sh` - is opaque,
        # and an opaque shell is the whole bypass, so it keeps the card.
        for index, arg in enumerate(args):
            if arg == "-c" and index + 1 < len(args):
                return is_destructive_command(args[index + 1])
        return not (args and all(a in _DESTRUCTIVE_QUERY_FLAGS for a in args))
    if name in _INTERPRETERS:
        # Arbitrary code, and no way to read it. Three shapes, all opaque:
        # a `-c`/`-e` payload, a program arriving on stdin (`python3 -`, a
        # heredoc, or `< script`), and a bare interpreter, which also reads
        # stdin. Running a script FILE stays approved - `python manage.py` is
        # ordinary use and carding it would card nearly every project command.
        if any(a in _INLINE_CODE_FLAGS for a in args):
            return True
        if any(a in _STDIN_OPERANDS for a in args) or _REDIRECT_IN in args:
            return True
        return not any(
            not a.startswith("-") and a != _REDIRECT_IN for a in args
        )
    if name == "git":
        return _git_is_destructive(args)
    if name == "find":
        return any(arg.startswith(_UNSAFE_FIND_PREFIXES) for arg in args)
    if name not in _DESTRUCTIVE_COMMANDS:
        return False
    # A help/version-only invocation of a destructive verb is a query.
    if args and all(arg in _DESTRUCTIVE_QUERY_FLAGS for arg in args):
        return False
    return True


def is_destructive_command(command: str) -> bool:
    """True when *command* can remove, truncate, or destroy data/state.

    A pipeline or `;`/`&&`/`||` chain is destructive when any segment is. A
    ``find`` invocation whose action deletes is destructive. A query flag on a
    destructive verb (`rm --help`) is not. Anything that cannot be tokenized
    cleanly is treated as *not* destructive: the permissive default leans
    toward allowing when in doubt, which is the intended flip from the
    read-only classifier's conservative bias.
    """
    text = (command or "").strip()
    if not text:
        return False
    # Classify anything embedded via `$(…)` or backticks on its own, then drop
    # it so the outer tokenization cannot smuggle the verb into another
    # segment's arguments.
    for outer, inner in _SUBSTITUTION_RE.findall(text):
        embedded = outer or inner
        if embedded and is_destructive_command(embedded):
            return True
    text = _SUBSTITUTION_RE.sub(" ", text).strip()
    if not text:
        return False
    tokens = _tokens(text)
    if not tokens:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _CONNECTORS:
            segments.append([])
        elif set(token) & {"(", ")"}:
            # A subshell is its own command, so parens START a segment rather
            # than being skipped - otherwise the verb inside lands in the outer
            # segment's arguments, where only segment[0] is ever classified.
            segments.append([])
        elif all(char in _PUNCTUATION for char in token):
            if "<" in token:
                # Keep the fact that input was redirected: an interpreter fed a
                # heredoc or a file is running code nothing here can inspect.
                segments[-1].append(_REDIRECT_IN)
            # Otherwise redirection out, background `&`: unknown but not removal.
            continue
        else:
            segments[-1].append(token)
    for segment in segments:
        if not segment:
            continue
        if _segment_is_destructive(segment[0], segment[1:]):
            return True
    return False
