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
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ciao.agent_surface import AGENT_TOKEN_ENV, AGENT_URL_ENV

#: Top-level words ``ciao.cli.main`` hands to this module unconditionally.
#: ``run`` and ``gws`` are operator commands too (the server launcher and the
#: gws passthrough), so those route only for the verbs in ``_SHARED_NOUN_VERBS``.
AGENT_NOUNS: frozenset[str] = frozenset(
    {"memory", "vault", "file", "chat", "project", "schedule", "workspace", "context", "help"}
)
_SHARED_NOUN_VERBS: dict[str, frozenset[str]] = {
    "run": frozenset({"start", "status", "cancel"}),
    "gws": frozenset({"status"}),
}

_SKILL_PATH = Path(__file__).resolve().parent / "stock" / "skills" / "ciao-cli" / "SKILL.md"

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
)

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


def is_agent_invocation(argv: list[str]) -> bool:
    """True when ``argv`` (without the program name) is an agent command.

    ``ciao run`` alone is the operator's server launcher and ``ciao gws
    <profile> <service>`` the gws passthrough; only ``run start|status|cancel``
    and a bare ``gws status`` belong to the agent surface.
    """
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
    review.add_parser("list")
    show = review.add_parser("show")
    show.add_argument("path")
    for verb in ("keep", "trash", "restore", "delete"):
        sub = review.add_parser(verb)
        sub.add_argument("--candidate", required=True)
        if verb == "delete":
            sub.add_argument("--confirm", required=True, help="Repeat the candidate id.")

    file_ = _verbs(nouns.add_parser("file", help="Surface a workspace file in the pinned panel."))
    surface = file_.add_parser("surface")
    surface.add_argument("path")

    chat = _verbs(nouns.add_parser("chat", help="Chats in the active workspace."))
    lst = chat.add_parser("list")
    lst.add_argument("--project", default="")
    for verb in ("get", "archive", "delete", "retry", "update", "handover"):
        sub = chat.add_parser(verb)
        sub.add_argument("--chat", default="", help="Defaults to the calling chat.")
        if verb == "retry":
            sub.add_argument("--action", default="try_now", choices=["set", "stop", "try_now"])
            sub.add_argument("--prompt", default="")
        elif verb == "update":
            for flag, _param in _CHAT_UPDATE_FLAGS:
                sub.add_argument(flag, default=None)
        elif verb == "handover":
            sub.add_argument("--provider", default="")
            sub.add_argument("--model", default="")
            sub.add_argument("--messages", default=None, help="Visible history to carry: @file.json or inline JSON.")
    create = chat.add_parser("create")
    create.add_argument("--project", default=None)
    create.add_argument("--title", default="New Chat")
    create.add_argument("--provider", default=None)
    create.add_argument("--model", default=None)
    create.add_argument("--mode", default=None)
    create.add_argument("--prompt", default=None)
    send = chat.add_parser("send")
    send.add_argument("--chat", required=True)
    send.add_argument("--prompt", required=True)
    for verb in ("stop", "continue"):
        sub = chat.add_parser(verb)
        sub.add_argument("--chat", required=True)

    project = _verbs(nouns.add_parser("project", help="Projects in the active workspace."))
    plist = project.add_parser("list")
    plist.add_argument("--include-completed", action="store_true")
    pget = project.add_parser("get")
    pget.add_argument("project_id", nargs="?", default="")
    pcreate = project.add_parser("create")
    pcreate.add_argument("--name", required=True)
    pcreate.add_argument("--context", default=None)
    pupdate = project.add_parser("update")
    pupdate.add_argument("project_id", nargs="?", default="")
    pupdate.add_argument("--name", default=None)
    pupdate.add_argument("--context", default=None)
    pupdate.add_argument("--vault-folder", default=None)
    prestore = project.add_parser("restore")
    prestore.add_argument("stem")
    for verb in ("complete", "delete"):
        sub = project.add_parser(verb)
        sub.add_argument("project_id")

    schedule = _verbs(nouns.add_parser("schedule", help="Schedules in the active workspace."))
    schedule.add_parser("list")
    for verb in ("create", "preview"):
        sub = schedule.add_parser(verb)
        for flag, _param, kind in _SCHEDULE_FLAGS:
            sub.add_argument(flag, default=None, type=(int if kind is int else str))
    upd = schedule.add_parser("update")
    upd.add_argument("schedule_id")
    for flag, _param, kind in _SCHEDULE_FLAGS:
        upd.add_argument(flag, default=None, type=(int if kind is int else str))
    for verb in ("pause", "resume", "run", "delete"):
        sub = schedule.add_parser(verb)
        sub.add_argument("schedule_id")

    run = _verbs(nouns.add_parser("run", help="Tracked background command runs."))
    start = run.add_parser("start", help="Run one command in a tracked background subprocess.")
    start.add_argument("--cwd", default=None)
    start.add_argument("--env", action="append", default=[], metavar="K=V")
    start.add_argument("--timeout-s", type=int, default=None)
    start.add_argument("--label", default=None)
    start.add_argument("cmd", nargs=argparse.REMAINDER, help="-- CMD [ARGS...]")
    status = run.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--lines", type=int, default=None)
    cancel = run.add_parser("cancel")
    cancel.add_argument("run_id")

    gws = _verbs(nouns.add_parser("gws", help="Google Workspace connection status."))
    gws.add_parser("status")

    workspace = _verbs(nouns.add_parser("workspace", help="Configured logical workspaces."))
    workspace.add_parser("list")
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
        text = Path(raw[1:]).read_text(encoding="utf-8") if raw.startswith("@") else raw
        messages = json.loads(text)
    except (OSError, ValueError) as exc:
        raise UsageError(f"--messages must be @file.json or inline JSON: {exc}") from exc
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise UsageError("--messages must be a JSON array of message objects.")
    return messages


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
        return "vault_review", {"action": action, "candidate_id": args.candidate}
    if noun == "file":
        return "file_surface", {"path": args.path}
    if noun == "chat":
        if verb == "list":
            return "chats_list", {"project_id": args.project}
        if verb in ("get", "archive", "delete", "stop", "continue"):
            return f"chat_{verb}", {"chat_id": args.chat}
        if verb == "create":
            payload = {k: getattr(args, k) for k in ("project", "title", "provider", "model", "mode", "prompt")}
            payload["project_id"] = payload.pop("project")
            return "chat_create", {k: v for k, v in payload.items() if v is not None}
        if verb == "send":
            return "chat_send", {"chat_id": args.chat, "prompt": args.prompt}
        if verb == "retry":
            return "chat_retry", {"chat_id": args.chat, "action": args.action, "prompt": args.prompt}
        if verb == "update":
            arguments: dict[str, Any] = {"chat_id": args.chat}
            for flag, param in _CHAT_UPDATE_FLAGS:
                value = getattr(args, flag.lstrip("-").replace("-", "_"), None)
                if value is not None:
                    arguments[param] = value
            return "chat_update", arguments
        if verb == "handover":
            arguments = {"chat_id": args.chat, "provider": args.provider, "model": args.model}
            messages = _handover_messages(args.messages)
            if messages is not None:
                arguments["messages"] = messages
            return "chat_handover", arguments
    if noun == "project":
        if verb == "list":
            return "projects_list", {"include_completed": bool(args.include_completed)}
        if verb == "get":
            return "project_get", {"project_id": args.project_id}
        if verb == "create":
            arguments = {"action": "create", "name": args.name}
            if args.context is not None:
                arguments["context"] = args.context
            return "project", arguments
        if verb == "update":
            arguments = {"action": "update", "project_id": args.project_id}
            for param in ("name", "context", "vault_folder"):
                value = getattr(args, param, None)
                if value is not None:
                    arguments[param] = value
            return "project", arguments
        if verb == "restore":
            return "project", {"action": "restore", "stem": args.stem}
        return "project_action", {"action": verb, "project_id": args.project_id}
    if noun == "schedule":
        if verb == "list":
            return "schedules_list", {}
        if verb in ("create", "update", "preview"):
            return "schedule", _schedule_arguments(args, verb)
        return "schedule_action", {"schedule_id": args.schedule_id, "action": verb}
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
    return None


def post(op: str, arguments: dict[str, Any], *, url: str, token: str, timeout_s: float = 60.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/" + op,
        data=json.dumps(arguments).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - loopback only
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


def main(argv: list[str]) -> int:
    parser = build_parser()
    # ``--json`` is documented as accepted anywhere; output is JSON regardless.
    args = parser.parse_args([token for token in argv if token != "--json"])
    if args.noun is None:
        parser.print_help()
        return 2
    if args.noun == "help":
        try:
            sys.stdout.write(_SKILL_PATH.read_text(encoding="utf-8"))
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
