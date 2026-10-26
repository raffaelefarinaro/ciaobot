"""``ciao <noun> <verb>``: the agent command line over ``/agent/v1/{op}``.

Runs inside a managed provider shell. Identity comes from ``CIAO_AGENT_TOKEN``
and ``CIAO_AGENT_URL`` in the environment, never from flags. Every command
prints exactly one JSON envelope on stdout and exits 0 when ``ok`` is true, 1
when the server answered ``ok: false``, 2 for a usage error caught before any
request is sent. Argument names mirror the control-plane tool parameters so the
telemetry ``tool`` field keeps its MCP-era name.

Kept import-light on purpose: ``ciao.cli`` dispatches here before building its
own (large) parser, so a ``ciao vault search`` costs a few tens of
milliseconds of interpreter start-up, not the operator CLI's import graph.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ciao.agent_surface import AGENT_TOKEN_ENV, AGENT_URL_ENV
from ciao.execution_modes import is_credential_adjacent
from ciao.terminal_offer import (
    TERMINAL_OFFER_MAX_BYTES,
    TerminalOfferError,
    normalize_terminal_offer,
)

#: Top-level words ``ciao.cli.main`` hands to this module unconditionally.
#: ``run`` and ``gws`` are operator commands too (the server launcher and the
#: gws passthrough), so those route only for the verbs in ``_SHARED_NOUN_VERBS``.
AGENT_NOUNS: frozenset[str] = frozenset(
    {
        "memory", "vault", "note", "file", "chat", "project", "task", "schedule",
        "workspace", "repo", "webhook", "context", "help",
    }
)
_REPO_HELP = "Absolute path (or one relative to this shell) of a local git checkout's top level; \"\" unlinks."
_SHARED_NOUN_VERBS: dict[str, frozenset[str]] = {
    "run": frozenset({"start", "status", "cancel"}),
    "gws": frozenset({"status"}),
}

_SKILL_PATH = Path(__file__).resolve().parent / "stock" / "skills" / "ciao-cli" / "SKILL.md"

_SCHEDULE_FLAG_HELP: dict[str, str] = {
    "--command": "Shell command run before the prompt. Only a bypass-mode chat may set one; "
    "otherwise ask the user to add it in the schedule's Prompt card.",
}

_SCHEDULE_FLAGS: tuple[tuple[str, str, Any], ...] = (
    # (flag, tool parameter, type)
    ("--prompt", "prompt", str),
    ("--daily-time", "daily_time", str),
    ("--timezone", "timezone", str),
    ("--frequency", "frequency", str),
    ("--interval-minutes", "interval_minutes", int),
    ("--days-of-week", "days_of_week", "csv"),
    ("--day-of-month", "day_of_month", int),
    ("--run-at-date", "run_at_date", str),
    ("--project", "project_id", str),
    ("--title", "title", str),
    ("--description", "description", str),
    ("--provider", "provider", str),
    ("--model", "model", str),
    ("--archive-policy", "archive_policy", str),
    ("--command", "command", str),
)

#: Rows `chat list` returns per page unless `--limit` says otherwise. The
#: control plane's ceiling is `PAGE_MAX_LIMIT`.
CHATS_LIST_DEFAULT_LIMIT = 20
#: Messages `chat get` returns by default (the most recent ones).
CHAT_GET_RECENT_MESSAGES = 10
#: Characters of one message's content `chat get` returns by default.
CHAT_MESSAGE_MAX_CHARS = 2000

_VAULT_REVIEW_HELP: dict[str, str] = {
    "keep": "Keep a note and stamp its updated date.",
    "trash": "Move a note to trash (reversible).",
    "restore": "Restore a trashed note.",
    "complete": "Close a project out into projects/completed/.",
    "restore-completed": "Undo a project completion.",
    "delete": "Permanently delete a trashed note.",
}
_CHAT_TARGET_HELP: dict[str, str] = {
    "get": "Get one chat's metadata and recent messages.",
    "archive": "Archive a chat to the vault.",
    "delete": "Delete a chat.",
    "retry": "Manage a deferred provider-limit retry.",
    "update": "Update chat metadata and model settings.",
    "handover": "Move a chat to a fresh provider session.",
    "reload": "Restart the chat's provider so changed MCP servers, plugins and skills load.",
}
_CHAT_EXPLICIT_HELP: dict[str, str] = {
    "stop": "Stop another chat's active turn.",
    "continue": "Continue an archived chat as a new chat.",
}

_CHAT_UPDATE_FLAGS: tuple[tuple[str, str], ...] = (
    ("--title", "title"),
    ("--provider", "provider"),
    ("--model", "model"),
    ("--mode", "mode"),
    ("--thinking-level", "thinking_level"),
    ("--project", "project_id"),
)


class UsageError(Exception):
    """A caller mistake caught before any request is sent (exit 2)."""


#: What a path operation can raise before it has produced a path: an I/O
#: error, the ``RuntimeError`` an unknown ``~user`` raises while the home
#: directory is looked up, and the ``ValueError`` an embedded NUL raises in
#: every OS call that takes the name. All three mean the same thing to a
#: caller — the file cannot be read — so all three are answered with one
#: static reason.
_PATH_FAILURES = (OSError, RuntimeError, ValueError)


def is_agent_invocation(argv: list[str]) -> bool:
    """True when ``argv`` (without the program name) is an agent command.

    ``ciao run`` alone is the operator's server launcher and ``ciao gws
    <profile> <service>`` the gws passthrough; only ``run start|status|cancel``
    and a bare ``gws status`` belong to the agent surface.
    """
    argv = _strip_json_flag(argv)
    if not argv:
        return False
    if argv[0] in AGENT_NOUNS:
        return True
    verbs = _SHARED_NOUN_VERBS.get(argv[0])
    if verbs is None or len(argv) < 2 or argv[1] not in verbs:
        return False
    return argv[0] != "gws" or len(argv) == 2


def _emit(envelope: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return 0 if envelope.get("ok") else 1


def _usage_error(parser: argparse.ArgumentParser, message: str) -> int:
    parser.print_usage(sys.stderr)
    sys.stderr.write(f"error: {message}\n")
    return 2


def _repo_path(raw: str | None) -> str | None:
    """`--repo` as the engine needs it: absolute, because this shell's cwd is not the engine's. "" (unlink) and None (keep) pass through."""
    if raw is None or not raw.strip():
        return raw
    return os.path.abspath(os.path.expanduser(raw.strip()))


def _verbs(parent: argparse.ArgumentParser, dest: str = "verb") -> argparse._SubParsersAction:
    """Required sub-command group, so a missing verb is argparse's own usage error."""
    return parent.add_subparsers(dest=dest, metavar=f"<{dest}>", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ciao",
        description="Ciaobot agent commands. Output is always one JSON envelope.",
    )
    parser.add_argument("--json", action="store_true", help="Accepted for compatibility; output is always JSON.")
    nouns = parser.add_subparsers(dest="noun", metavar="<noun>")

    nouns.add_parser("help", help="Print the ciao-cli command reference.")

    context = _verbs(nouns.add_parser("context", help="Workspace, project and chat context."))
    context.add_parser("get", help="Return the active workspace, project, chat and server status.")
    logs = context.add_parser("logs", help="Return Ciaobot's version and recent error, debug and service log tails.")
    logs.add_argument("--lines", type=int, default=200, help="Lines per log, 1-1000.")

    memory = _verbs(nouns.add_parser("memory", help="Bounded native-guide memory."))
    memory.add_parser("status", help="Memory usage and diagnostics.")
    update = memory.add_parser("update", help="Add, replace or remove one entry.")
    update.add_argument("--region", required=True, choices=["memory", "profile"])
    update.add_argument("--action", required=True, choices=["add", "replace", "remove"])
    update.add_argument("--entry", default="", help="New text for add/replace.")
    update.add_argument("--match", default="", help="Existing entry text for replace/remove.")

    vault = _verbs(nouns.add_parser("vault", help="Vault search and review."))
    search = vault.add_parser("search", help="Full-text search of the active vault.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    review = _verbs(vault.add_parser("review", help="Vault-note review queue."), dest="action")
    review.add_parser("list", help="List review candidates (stale, orphaned, duplicate, weakly linked).")
    show = review.add_parser("show", help="Show one candidate's evidence.")
    show.add_argument("path")
    # `restore-completed` is hyphenated like the rest of the verb surface; the
    # action it maps to is the review module's own `restore_completed`.
    for verb, verb_help in _VAULT_REVIEW_HELP.items():
        sub = review.add_parser(verb, help=verb_help)
        sub.add_argument("--candidate", required=True)
        if verb == "delete":
            sub.add_argument("--confirm", required=True, help="Repeat the candidate id.")

    note = _verbs(nouns.add_parser("note", help="Verification of stale vault notes."))
    related = note.add_parser("related", help="List the vault notes connected to one note.")
    related.add_argument("path", help="The note, vault-relative (People/Sofia.md).")
    related.add_argument("--limit", type=int, default=15, help="At most this many related notes (1-50).")
    verify = note.add_parser("verify", help="Record one stale note's verification verdict.")
    # A path, not the payload itself. The verdict's before/after text and its
    # citations are user prose: as a shell argument it would be mangled by
    # `$()`, backticks and quotes, and it would sit in the process table. The
    # same rule as `skill-proposal-add --input-file` and `chat handover
    # --messages`, and the server reads and bounds the document rather than the
    # caller reading it and posting the parsed dict.
    verify.add_argument(
        "--payload-file",
        required=True,
        metavar="FILE",
        help="JSON document holding the verdict: relative_path, expected_revision, "
        "outcome, coverage, evidence, before/after, reason. Never pass the prose itself.",
    )

    file_ = _verbs(nouns.add_parser("file", help="Surface a workspace file in the pinned panel."))
    surface = file_.add_parser("surface", help="Open a workspace file in the pinned panel.")
    surface.add_argument("path")

    chat = _verbs(nouns.add_parser("chat", help="Chats in the active workspace."))
    lst = chat.add_parser("list", help="List chats, newest activity first.")
    lst.add_argument("--project", default="")
    lst.add_argument("--limit", type=int, default=CHATS_LIST_DEFAULT_LIMIT, help="Rows per page, 1-200.")
    lst.add_argument("--offset", type=int, default=0, help="Rows to skip (newest first); use next_offset to page on.")
    lst.add_argument("--full", action="store_true", help="Full rows instead of the compact listing fields.")
    for verb, verb_help in _CHAT_TARGET_HELP.items():
        sub = chat.add_parser(verb, help=verb_help)
        sub.add_argument("--chat", default="", help="Defaults to the calling chat.")
        if verb == "get":
            sub.add_argument(
                "--full",
                action="store_true",
                help="Every message, untruncated, instead of the last few capped messages.",
            )
        if verb == "retry":
            sub.add_argument("--action", default="try_now", choices=["set", "stop", "try_now"])
            sub.add_argument("--prompt", default="")
            sub.add_argument("--kind", default="", choices=["network"], help="With --action set: re-run this chat's prompt after a network outage (10, 20, 40 min; at most 3 tries).")
        elif verb == "update":
            for flag, _param in _CHAT_UPDATE_FLAGS:
                sub.add_argument(flag, default=None)
            sub.add_argument(
                "--code",
                choices=("on", "off"),
                default=None,
                help="Code mode for a chat in a project linked to a repository.",
            )
        elif verb == "handover":
            sub.add_argument("--provider", default="")
            sub.add_argument("--model", default="")
            sub.add_argument("--messages", default=None, help="Visible history to carry: @file.json or inline JSON.")
    create = chat.add_parser("create", help="Create a chat, optionally with a first prompt.")
    create.add_argument("--project", default=None)
    create.add_argument("--title", default="New Chat")
    create.add_argument("--provider", default=None)
    create.add_argument("--model", default=None)
    create.add_argument("--mode", default=None)
    create.add_argument("--prompt", default=None)
    send = chat.add_parser("send", help="Start or queue a user turn in a chat.")
    send.add_argument("--chat", default="", help="Defaults to the calling chat.")
    send.add_argument("--prompt", required=True)
    for verb, verb_help in _CHAT_EXPLICIT_HELP.items():
        sub = chat.add_parser(verb, help=verb_help)
        # Target-only operations: stopping the calling chat hits
        # ``self_stop_forbidden``, and continuing requires an *archived* chat,
        # so the calling chat is never a valid default. Make the target
        # explicit rather than advertising a default that can only error.
        sub.add_argument("--chat", required=True)
    # The one chat verb with no `--chat`: an offer is a command on somebody's
    # screen, and the screen is the chat that asked for it.
    offer = chat.add_parser(
        "terminal-offer",
        help="Suggest a terminal command for the calling chat.",
        # No abbreviation: `--command` would otherwise be an accepted spelling
        # of `--command-file`, and a near-miss that reads as a path is a
        # command offered by accident.
        allow_abbrev=False,
    )
    offer.add_argument(
        "--command-file",
        required=True,
        metavar="FILE",
        help="File holding the command to suggest: @file.txt or a plain path. "
        "The text is shown to the user to review and run; nothing is executed.",
    )

    project = _verbs(nouns.add_parser("project", help="Projects in the active workspace."))
    plist = project.add_parser("list", help="List projects.")
    plist.add_argument("--include-completed", action="store_true")
    pget = project.add_parser("get", help="Get one project by id or name.")
    pget.add_argument("project_id", nargs="?", default="")
    pcreate = project.add_parser("create", help="Create a project.")
    pcreate.add_argument("--name", required=True)
    pcreate.add_argument("--context", default=None)
    pcreate.add_argument("--repo", dest="repo_path", default=None, help=_REPO_HELP)
    pupdate = project.add_parser("update", help="Update project metadata or its repository link.")
    pupdate.add_argument("project_id", nargs="?", default="")
    pupdate.add_argument("--name", default=None)
    pupdate.add_argument("--context", default=None)
    pupdate.add_argument("--vault-folder", default=None)
    pupdate.add_argument("--repo", dest="repo_path", default=None, help=_REPO_HELP)
    prestore = project.add_parser("restore", help="Restore a completed vault project.")
    prestore.add_argument("stem")
    for verb, verb_help in (
        ("complete", "Move a vault-backed project to completed."),
        ("delete", "Delete a non-vault-backed project and its chats."),
    ):
        sub = project.add_parser(verb, help=verb_help)
        sub.add_argument("project_id")

    task = _verbs(nouns.add_parser("task", help="Tasks in the active workspace."))
    tlist = task.add_parser("list", help="List the board.")
    tlist.add_argument("--completed-since", default=None, metavar="ISO-8601", help="Keep tasks completed at or after this time (inclusive).")
    tlist.add_argument("--completed-before", default=None, metavar="ISO-8601", help="Keep tasks completed before this time (exclusive).")
    tget = task.add_parser("get", help="Get one task with its body.")
    tget.add_argument("task_id")
    tcreate = task.add_parser("create", help="File a task.")
    tcreate.add_argument("--title", required=True)
    # The body is Markdown prose (description, links, acceptance criteria):
    # a path, for the same reason `note verify --payload-file` is one.
    tcreate.add_argument("--body-file", default=None, metavar="FILE", help="Markdown file holding the task body: @file.md or a plain path.")
    tcreate.add_argument("--project", default=None)
    tcreate.add_argument("--due", default=None, metavar="YYYY-MM-DD")
    tupdate = task.add_parser("update", help="Edit one task; only the passed fields change.")
    tupdate.add_argument("task_id")
    tupdate.add_argument("--revision", required=True, help="The revision you read; a stale one changes nothing.")
    tupdate.add_argument("--title", default=None)
    tupdate.add_argument("--body-file", default=None, metavar="FILE")
    tupdate.add_argument("--due", default=None, metavar="YYYY-MM-DD")
    tupdate.add_argument("--assignee", default=None, choices=["user", "agent"])
    tupdate.add_argument("--status", default=None, choices=["backlog", "in_progress", "in_review", "done"])
    tdelete = task.add_parser("delete", help="Permanently remove one task; its chats and runs stay.")
    tdelete.add_argument("task_id")
    tdelete.add_argument("--revision", required=True, help="The revision you read; a stale one removes nothing.")
    tdelete.add_argument("--reason-file", required=True, metavar="FILE", help=(
        "Text file holding why you are deleting it, in the user's words: "
        "@file.txt or a plain path."
    ))
    tmove = task.add_parser("move", help="Set a task's column.")
    tmove.add_argument("task_id")
    tmove.add_argument("--to", required=True, choices=["backlog", "in_progress", "in_review", "done"])
    tmove.add_argument("--revision", required=True)
    tcomplete = task.add_parser("complete", help="Mark a task done because the user said it is.")
    tcomplete.add_argument("task_id")
    tcomplete.add_argument("--revision", required=True)
    tcomplete.add_argument("--reason-file", required=True, metavar="FILE", help=(
        "Text file holding why it is done, in the user's words: "
        "@file.txt or a plain path."
    ))
    tdelegate = task.add_parser("delegate", help="Hand a task to the agent as one chat.")
    tdelegate.add_argument("task_id")
    tdelegate.add_argument("--revision", required=True, help="The revision you read; a stale one starts nothing.")
    tdelegate.add_argument("--project", default=None, help="Override the chat's project; omit to use the task's own, else General.")
    tnote = task.add_parser("add-update", help="Append a progress note. This does not complete the task.")
    tnote.add_argument("task_id")
    tnote.add_argument("--revision", required=True, help="The revision you read; a stale one changes nothing.")
    tnote.add_argument("--text-file", required=True, metavar="FILE", help="Markdown file holding the progress note.")
    tlink = task.add_parser("link-chat", help="Link a chat (default: this one) to a task, or --remove it. Not a delegation.")
    tlink.add_argument("task_id")
    tlink.add_argument("--revision", required=True, help="The revision you read; a stale one changes nothing.")
    tlink.add_argument("--chat", default=None, help="Chat id or exact title; omit for this chat.")
    tlink.add_argument("--remove", action="store_true")
    treport = task.add_parser("report", help="Report how far you got on the task this chat was handed.")
    treport.add_argument("task_id")
    treport.add_argument("--outcome", required=True, choices=["done", "blocked", "needs_input"])
    treport.add_argument("--summary-file", required=True, metavar="FILE", help="Markdown: what you did, where the results are, what is left. @file.md or a plain path.")
    tattempt = task.add_parser("attempt", help="Stop, resume, retry or detach a delegated attempt.")
    tattempt.add_argument("attempt_id")
    tattempt.add_argument(
        "action", choices=["stop", "resume", "retry", "detach"]
    )
    tattempt.add_argument("--reason-file", default=None, metavar="FILE", help=(
        "Optional, for `detach` of a result that is ready for review: "
        "why it is being released."
    ))

    schedule = _verbs(nouns.add_parser("schedule", help="Schedules in the active workspace."))
    schedule.add_parser("list", help="List schedules with their next run.")
    for verb, verb_help in (
        ("create", "Create a validated schedule."),
        ("preview", "Validate fields and compute next_run without saving."),
    ):
        sub = schedule.add_parser(verb, help=verb_help)
        for flag, _param, kind in _SCHEDULE_FLAGS:
            sub.add_argument(
                flag, default=None, type=(int if kind is int else str),
                help=_SCHEDULE_FLAG_HELP.get(flag),
            )
    upd = schedule.add_parser("update", help="Update a schedule; only the passed fields change.")
    upd.add_argument("schedule_id")
    for flag, _param, kind in _SCHEDULE_FLAGS:
        upd.add_argument(
            flag, default=None, type=(int if kind is int else str),
            help=_SCHEDULE_FLAG_HELP.get(flag),
        )
    for verb, verb_help in (
        ("pause", "Pause a schedule without deleting it."),
        ("resume", "Resume a paused schedule."),
        ("run", "Dispatch a schedule now."),
        ("delete", "Delete a removable user schedule."),
    ):
        sub = schedule.add_parser(verb, help=verb_help)
        sub.add_argument("schedule_id")

    webhook = _verbs(nouns.add_parser("webhook", help="Webhook triggers in the active workspace."))
    webhook.add_parser("list", help="List triggers as public records.")
    wcreate = webhook.add_parser("create", help="Create a disabled trigger and mint its secret (shown once).")
    wcreate.add_argument("--name", required=True)
    # The instructions are operator prose that the launched turn runs as: a
    # path, for the same reason `task create --body-file` is one.
    wcreate.add_argument(
        "--instructions-file",
        default=None,
        metavar="FILE",
        help="Markdown/text file holding the trigger's instructions: @file.md or a plain path.",
    )
    wcreate.add_argument("--project", default=None, metavar="P")
    wupdate = webhook.add_parser("update", help="Edit a trigger, or enable/disable it.")
    wupdate.add_argument("trigger_id")
    wupdate.add_argument("--revision", required=True, help="The revision you read; a stale one changes nothing.")
    wupdate.add_argument("--name", default=None)
    wupdate.add_argument("--instructions-file", default=None, metavar="FILE")
    # Two flags rather than `--enabled true|false`: a boolean option cannot say
    # "leave it alone", and a trigger that was enabled has to be stoppable
    # without touching its name or instructions.
    wupdate.add_argument("--enable", action="store_true", help="Make the trigger callable.")
    wupdate.add_argument("--disable", action="store_true", help="Stop the trigger, including events already accepted.")
    for verb, verb_help in (
        ("rotate", "Replace the secret; the old one stops working."),
        ("delete", "Delete the trigger."),
    ):
        sub = webhook.add_parser(verb, help=verb_help)
        sub.add_argument("trigger_id")
        sub.add_argument("--revision", required=True)

    run = _verbs(nouns.add_parser("run", help="Tracked background command runs."))
    start = run.add_parser("start", help="Run one command in a tracked background subprocess.")
    start.add_argument("--cwd", default=None)
    start.add_argument("--env", action="append", default=[], metavar="K=V")
    start.add_argument("--timeout-s", type=int, default=None)
    start.add_argument("--label", default=None)
    start.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD [ARGS...]")
    status = run.add_parser("status", help="Status and log tail of a run this chat started.")
    status.add_argument("run_id")
    status.add_argument("--lines", type=int, default=None)
    cancel = run.add_parser("cancel", help="Cancel a running background command.")
    cancel.add_argument("run_id")

    gws = _verbs(nouns.add_parser("gws", help="Google Workspace connection status."))
    gws.add_parser("status", help="Report whether the Google Workspace login is valid.")

    workspace = _verbs(nouns.add_parser("workspace", help="Configured logical workspaces."))
    workspace.add_parser("list", help="List the configured workspaces.")

    repo = _verbs(nouns.add_parser("repo", help="Local git checkouts on this machine."))
    repo.add_parser(
        "candidates",
        help="List git checkouts one level under ~/repos, ~/code, ~/src, ~/dev, ~/Developer, ~/projects.",
    )
    return parser


def _schedule_arguments(args: argparse.Namespace, action: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {"action": action}
    for flag, param, kind in _SCHEDULE_FLAGS:
        value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
        if value is None:
            continue
        if kind == "csv":
            value = [part.strip() for part in str(value).split(",") if part.strip()]
        arguments[param] = value
    if getattr(args, "schedule_id", None):
        arguments["schedule_id"] = args.schedule_id
    return arguments


def _handover_messages(raw: str | None) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    try:
        if raw.startswith("@"):
            # Same credential boundary as the provider file tools: an `@`
            # read needs only one generic `ciao …` approval, so without this
            # a workspace `.env` (or anything under `.runtime/`/`secrets/`)
            # could be embedded into a vault-persisted handover with no more
            # consent than the command itself. An agent with shell access can
            # `cat` anyway; this path must not make it cheaper.
            candidate = Path(raw[1:]).expanduser()
            resolved = candidate.resolve()
            if is_credential_adjacent(resolved):
                raise UsageError(
                    f"--messages refuses credential-adjacent paths ({raw[1:]}): "
                    "pass the content inline instead."
                )
            text = resolved.read_text(encoding="utf-8")
        else:
            text = raw
        messages = json.loads(text)
    except (OSError, ValueError) as exc:
        raise UsageError(f"--messages must be @file.json or inline JSON: {exc}") from exc
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise UsageError("--messages must be a JSON array of message objects.")
    return messages


def _task_body(raw: str | None, *, flag: str = "--body-file") -> str | None:
    """The task body from a Markdown file, or None when no file was given.

    Read here rather than posted as a path because a task body is Markdown
    prose, exactly the case `--payload-file` and `--messages` already handle:
    as a shell argument it would be mangled by `$()`, backticks and quotes,
    and it would sit in the process table. The same credential-adjacency check
    applies, because a task body is written into the vault.

    ``flag`` is the name to answer with: a refusal that says `--body-file` to a
    caller who passed `--instructions-file` is a wrong answer, and these two
    read the same kind of prose for the same reason.
    """
    if raw is None:
        return None
    path = Path(raw[1:]).expanduser() if raw.startswith("@") else Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise UsageError(f"{flag} cannot be read: {exc}") from exc
    if is_credential_adjacent(resolved):
        raise UsageError(
            f"{flag} refuses credential-adjacent paths ({path}): "
            "pass the content in a workspace file instead."
        )
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise UsageError(f"{flag} must be a readable UTF-8 file: {exc}") from exc


def _webhook_instructions(raw: str | None) -> str | None:
    """The trigger's instructions from a file, or None when no file was given.

    Same rule as ``--body-file``: instructions are operator prose that the
    launched turn reads as its own, so as a shell argument they would be
    mangled by ``$()``, backticks and quotes and would sit in the process table.
    """
    return _task_body(raw, flag="--instructions-file")


def _terminal_offer_command(raw: str) -> str:
    """The suggested command read from a file, validated and normalized.

    The command is untrusted, model-authored text, so it travels in a file for
    the same reason prose does: as a shell argument it would be mangled by
    ``$()``, backticks and quotes, and it would sit in the process table. It is
    read with a bound (a command is a copyable snippet, not a file) and
    decoded strictly.

    The file has to be an ordinary one. A FIFO, a device or a socket answers
    an open with a read that never ends, and the byte bound does not help:
    the read blocks before it counts anything. So the mode is checked first
    and a non-regular file is refused without being opened.

    Every refusal is static: neither the path nor the text appears in it. A
    command can carry a credential, and an error string ends up in a log line
    and in the agent's transcript.
    """
    try:
        path = Path(raw[1:]).expanduser() if raw.startswith("@") else Path(raw).expanduser()
        resolved = path.resolve()
    except _PATH_FAILURES:
        # A name that cannot become a path: an unknown ``~user`` (a
        # ``RuntimeError``), an embedded NUL (a ``ValueError``), or an I/O
        # error. The reason says the file is unreadable and names nothing.
        raise UsageError("--command-file cannot be read.") from None
    if is_credential_adjacent(resolved):
        raise UsageError("--command-file refuses a credential-adjacent path.")
    try:
        mode = resolved.stat().st_mode
    except _PATH_FAILURES:
        raise UsageError("--command-file cannot be read.") from None
    if not stat.S_ISREG(mode):
        # Refused before the open: this is the check that keeps a FIFO from
        # blocking the read that would follow it.
        raise UsageError("--command-file must be an ordinary file.")
    try:
        with resolved.open("rb") as handle:
            # The handle has to be an ordinary file too. This does not close
            # the window between the two checks — the stat above is what
            # keeps a FIFO from blocking the open — it only refuses a handle
            # that is not an ordinary file.
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise UsageError("--command-file must be an ordinary file.")
            payload = handle.read(TERMINAL_OFFER_MAX_BYTES + 1)
    except _PATH_FAILURES:
        raise UsageError("--command-file cannot be read.") from None
    if len(payload) > TERMINAL_OFFER_MAX_BYTES:
        raise UsageError("--command-file holds more text than a command may.")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise UsageError("--command-file must be UTF-8 text.") from None
    try:
        return normalize_terminal_offer(text)
    except TerminalOfferError as exc:
        if exc.reason == "empty":
            raise UsageError("--command-file holds no command text.") from None
        raise UsageError("--command-file holds text a command may not contain.") from None


def _run_start_arguments(args: argparse.Namespace) -> dict[str, Any]:
    cmd = list(args.cmd)
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        raise UsageError("run start needs a command after `--`.")
    arguments: dict[str, Any] = {"cmd": cmd}
    env: dict[str, str] = {}
    for item in args.env:
        key, sep, value = str(item).partition("=")
        if not sep or not key:
            raise UsageError(f"--env expects K=V, got {item!r}.")
        env[key] = value
    if env:
        arguments["env"] = env
    for param in ("cwd", "timeout_s", "label"):
        option = getattr(args, param, None)
        if option is not None:
            arguments[param] = option
    return arguments


def resolve(args: argparse.Namespace) -> tuple[str, dict[str, Any]] | None:
    """Map parsed arguments to ``(operation, arguments)``; ``None`` for help.

    Raises :class:`UsageError` for a mistake argparse cannot see (a malformed
    ``--messages`` document, ``--env`` without ``=``, ``run start`` without a
    command).
    """
    noun, verb = args.noun, getattr(args, "verb", None)
    if noun == "help":
        return None
    if noun == "context":
        if verb == "logs":
            return "context_logs", {"lines": args.lines}
        return "context_get", {}
    if noun == "memory":
        if verb == "status":
            return "memory_status", {}
        return "memory_update", {"region": args.region, "action": args.action, "entry": args.entry, "match": args.match}
    if noun == "vault":
        if verb == "search":
            return "vault_search", {"query": args.query, "limit": args.limit}
        action = args.action
        if action == "list":
            return "vault_review", {"action": "list"}
        if action == "show":
            return "vault_review", {"action": "inspect", "path": args.path}
        if action == "keep":
            return "vault_review", {"action": "decide", "candidate_id": args.candidate, "disposition": "keep"}
        if action == "delete":
            return "vault_review", {"action": "delete", "candidate_id": args.candidate, "confirm": args.confirm}
        # Every remaining verb is its own action, and one is not spelled the same
        # way: the CLI surface is hyphenated (`restore-completed`) while the
        # review module's action is `restore_completed`. The verb stays as typed
        # in `args.action`, so the translation belongs here rather than in the
        # parser.
        wire = "restore_completed" if action == "restore-completed" else action
        return "vault_review", {"action": wire, "candidate_id": args.candidate}
    if noun == "note":
        if verb == "related":
            return "note_related", {"path": args.path, "limit": args.limit}
        # The path crosses to the server as written. Reading the document here
        # and posting the parsed object would move the size cap, the workspace
        # confinement and the "is this actually a file" check into the one place
        # that can be bypassed, for no gain: the payload is user prose the shell
        # must not touch, and a path is not prose.
        return "verify_note", {"payload_file": args.payload_file}
    if noun == "file":
        return "file_surface", {"path": args.path}
    if noun == "chat":
        if verb == "list":
            return "chats_list", {
                "project_id": args.project,
                "limit": args.limit,
                "offset": args.offset,
                "compact": not args.full,
            }
        if verb == "get":
            if args.full:
                return "chat_get", {"chat_id": args.chat, "messages": 0}
            return "chat_get", {
                "chat_id": args.chat,
                "messages": CHAT_GET_RECENT_MESSAGES,
                "message_chars": CHAT_MESSAGE_MAX_CHARS,
            }
        if verb in ("archive", "delete", "stop", "continue"):
            return f"chat_{verb}", {"chat_id": args.chat}
        if verb == "create":
            payload = {k: getattr(args, k) for k in ("project", "title", "provider", "model", "mode", "prompt")}
            payload["project_id"] = payload.pop("project")
            return "chat_create", {k: v for k, v in payload.items() if v is not None}
        if verb == "send":
            return "chat_send", {"chat_id": args.chat, "prompt": args.prompt}
        if verb == "retry":
            retry_args: dict[str, Any] = {"chat_id": args.chat, "action": args.action, "prompt": args.prompt}
            if args.kind:
                retry_args["kind"] = args.kind
            return "chat_retry", retry_args
        if verb == "update":
            arguments: dict[str, Any] = {"chat_id": args.chat}
            for flag, param in _CHAT_UPDATE_FLAGS:
                value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
                if value is not None:
                    arguments[param] = value
            if args.code is not None:
                arguments["code_mode"] = args.code == "on"
            return "chat_update", arguments
        if verb == "reload":
            return "chat_reload", {"chat_id": args.chat}
        if verb == "handover":
            arguments = {"chat_id": args.chat, "provider": args.provider, "model": args.model}
            messages = _handover_messages(args.messages)
            if messages is not None:
                arguments["messages"] = messages
            return "chat_handover", arguments
        if verb == "terminal-offer":
            # No chat id: the offer belongs to the calling chat, and the
            # command travels as text read here rather than as an argument.
            return "chat_terminal_offer", {
                "command": _terminal_offer_command(args.command_file)
            }
    if noun == "project":
        if verb == "list":
            return "projects_list", {"include_completed": bool(args.include_completed)}
        if verb == "get":
            return "project_get", {"project_id": args.project_id}
        if verb == "create":
            arguments = {"action": "create", "name": args.name}
            if args.context is not None:
                arguments["context"] = args.context
            repo_path = _repo_path(args.repo_path)
            if repo_path is not None:
                arguments["repo_path"] = repo_path
            return "project", arguments
        if verb == "update":
            arguments = {"action": "update", "project_id": args.project_id}
            for param in ("name", "context", "vault_folder"):
                value = getattr(args, param, None)
                if value is not None:
                    arguments[param] = value
            repo_path = _repo_path(args.repo_path)
            if repo_path is not None:
                arguments["repo_path"] = repo_path
            return "project", arguments
        if verb == "restore":
            return "project", {"action": "restore", "stem": args.stem}
        return "project_action", {"action": verb, "project_id": args.project_id}
    if noun == "task":
        if verb == "list":
            window: dict[str, str] = {}
            if args.completed_since is not None:
                window["completed_since"] = args.completed_since
            if args.completed_before is not None:
                window["completed_before"] = args.completed_before
            return "task_list", window
        if verb == "get":
            return "task_get", {"task_id": args.task_id}
        if verb == "create":
            arguments = {"title": args.title}
            body = _task_body(args.body_file)
            if body is not None:
                arguments["body"] = body
            if args.project is not None:
                arguments["project_id"] = args.project
            if args.due is not None:
                arguments["due"] = args.due
            return "task_create", arguments
        if verb == "update":
            arguments = {"task_id": args.task_id, "expected_revision": args.revision}
            for param in ("title", "due", "assignee", "status"):
                value = getattr(args, param)
                if value is not None:
                    arguments[param] = value
            body = _task_body(args.body_file)
            if body is not None:
                arguments["body"] = body
            return "task_update", arguments
        if verb == "delete":
            arguments = {"task_id": args.task_id, "expected_revision": args.revision}
            arguments["reason"] = _task_body(args.reason_file, flag="--reason-file") or ""
            return "task_delete", arguments
        if verb == "move":
            return "task_action", {
                "action": "move",
                "task_id": args.task_id,
                "status": args.to,
                "expected_revision": args.revision,
            }
        if verb == "delegate":
            arguments = {"task_id": args.task_id, "expected_revision": args.revision}
            if args.project is not None:
                arguments["project_id"] = args.project
            return "task_delegate", arguments
        if verb == "link-chat":
            arguments = {"task_id": args.task_id, "expected_revision": args.revision, "remove": bool(args.remove)}
            if args.chat is not None:
                arguments["chat_id"] = args.chat
            return "task_link_chat", arguments
        if verb == "add-update":
            return "task_add_update", {
                "task_id": args.task_id,
                "expected_revision": args.revision,
                "text": _task_body(args.text_file, flag="--text-file") or "",
            }
        if verb == "report":
            return "task_report", {
                "task_id": args.task_id,
                "outcome": args.outcome,
                "summary": _task_body(args.summary_file, flag="--summary-file") or "",
            }
        if verb == "attempt":
            arguments = {"attempt_id": args.attempt_id, "action": args.action}
            reason = _task_body(args.reason_file, flag="--reason-file")
            if reason is not None:
                arguments["reason"] = reason
            return "task_attempt_action", arguments
        return "task_action", {
            "action": "complete",
            "task_id": args.task_id,
            "expected_revision": args.revision,
            "reason": _task_body(args.reason_file, flag="--reason-file") or "",
        }
    if noun == "schedule":
        if verb == "list":
            return "schedules_list", {}
        if verb in ("create", "update", "preview"):
            return "schedule", _schedule_arguments(args, verb)
        return "schedule_action", {"schedule_id": args.schedule_id, "action": verb}
    if noun == "webhook":
        if verb == "list":
            return "webhook_list", {}
        if verb == "create":
            arguments = {"name": args.name}
            instructions = _webhook_instructions(args.instructions_file)
            if instructions is not None:
                arguments["instructions"] = instructions
            if args.project is not None:
                arguments["project_id"] = args.project
            return "webhook_create", arguments
        if verb == "update":
            arguments = {
                "trigger_id": args.trigger_id,
                "expected_revision": args.revision,
            }
            if args.name is not None:
                arguments["name"] = args.name
            instructions = _webhook_instructions(args.instructions_file)
            if instructions is not None:
                arguments["instructions"] = instructions
            if args.enable and args.disable:
                raise UsageError("webhook update takes --enable or --disable, not both.")
            if args.enable:
                arguments["enabled"] = True
            elif args.disable:
                arguments["enabled"] = False
            return "webhook_update", arguments
        # `rotate` and `delete` differ only in what they do to the trigger, and
        # both are revision-checked edits of one record, so they read the same
        # arguments here and the operation table says which is which.
        return f"webhook_{verb}", {
            "trigger_id": args.trigger_id,
            "expected_revision": args.revision,
        }
    if noun == "run":
        if verb == "start":
            return "background_run_start", _run_start_arguments(args)
        if verb == "status":
            arguments = {"run_id": args.run_id}
            if args.lines is not None:
                arguments["lines"] = args.lines
            return "background_run_status", arguments
        return "background_run_cancel", {"run_id": args.run_id}
    if noun == "gws":
        return "gws_status", {}
    if noun == "workspace":
        return "workspaces_list", {}
    if noun == "repo":
        return "repo_candidates", {}
    return None


# The engine is on this machine and refuses a loopback peer that carries proxy
# headers, so the call must never be routed through a system or env HTTP proxy
# (urllib honours both, and a Mac proxy exception list rarely names 127.0.0.1).
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(op: str, arguments: dict[str, Any], *, url: str, token: str, timeout_s: float = 60.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/" + op,
        data=json.dumps(arguments).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with _LOCAL_OPENER.open(request, timeout=timeout_s) as response:  # noqa: S310 - loopback only
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": {"code": "unreachable", "message": f"Ciaobot is not reachable: {exc}", "retryable": True}}
    try:
        parsed = json.loads(body)
    except ValueError:
        return {"ok": False, "error": {"code": "bad_response", "message": body[:300], "retryable": True}}
    return parsed if isinstance(parsed, dict) else {"ok": True, "data": parsed}


def _strip_json_flag(argv: list[str]) -> list[str]:
    """Drop the compatibility ``--json`` from the ``ciao`` part of ``argv`` only.

    Everything after the first bare ``--`` is the background command's own
    argv (``ciao run start -- python report.py --json``) and is passed through
    untouched, as the command contract promises.
    """
    try:
        cut = argv.index("--")
    except ValueError:
        cut = len(argv)
    return [token for token in argv[:cut] if token != "--json"] + argv[cut:]


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(_strip_json_flag(argv))
    if args.noun is None:
        parser.print_help()
        return 2
    if args.noun == "help":
        try:
            sys.stdout.write(_SKILL_PATH.read_text(encoding="utf-8"))
            sys.stdout.write("\n" + _SKILL_PATH.with_name("reference.md").read_text(encoding="utf-8"))
        except OSError:
            parser.print_help()
        return 0
    try:
        resolved = resolve(args)
    except UsageError as exc:
        return _usage_error(parser, str(exc))
    if resolved is None:
        return _usage_error(parser, f"'{args.noun}' needs a verb; try `ciao {args.noun} --help`.")
    op, arguments = resolved
    token = os.environ.get(AGENT_TOKEN_ENV, "")
    url = os.environ.get(AGENT_URL_ENV, "")
    if not token or not url:
        return _emit({
            "ok": False,
            "error": {
                "code": "no_agent_session",
                "message": "ciao agent commands run inside a Ciaobot chat; no agent session is present in this shell.",
                "retryable": False,
            },
        })
    return _emit(post(op, arguments, url=url, token=token))
