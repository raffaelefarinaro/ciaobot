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

#: Top-level words ``ciao.cli.main`` hands to this module.
AGENT_NOUNS: frozenset[str] = frozenset({"memory", "vault", "file", "chat", "schedule", "context", "help"})

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


def _emit(envelope: dict[str, Any]) -> int:
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return 0 if envelope.get("ok") else 1


def _usage_error(parser: argparse.ArgumentParser, message: str) -> int:
    parser.print_usage(sys.stderr)
    sys.stderr.write(f"error: {message}\n")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ciao",
        description="Ciaobot agent commands. Output is always one JSON envelope.",
    )
    parser.add_argument("--json", action="store_true", help="Accepted for compatibility; output is always JSON.")
    nouns = parser.add_subparsers(dest="noun", metavar="<noun>")

    nouns.add_parser("help", help="Print the ciao-cli command reference.")

    context = nouns.add_parser("context", help="Workspace, project and chat context.").add_subparsers(dest="verb", metavar="<verb>")
    context.add_parser("get", help="Return the active workspace, project, chat and server status.")

    memory = nouns.add_parser("memory", help="Bounded native-guide memory.").add_subparsers(dest="verb", metavar="<verb>")
    memory.add_parser("status", help="Memory usage and diagnostics.")
    update = memory.add_parser("update", help="Add, replace or remove one entry.")
    update.add_argument("--region", required=True, choices=["memory", "profile"])
    update.add_argument("--action", required=True, choices=["add", "replace", "remove"])
    update.add_argument("--entry", default="", help="New text for add/replace.")
    update.add_argument("--match", default="", help="Existing entry text for replace/remove.")

    vault = nouns.add_parser("vault", help="Vault search and review.").add_subparsers(dest="verb", metavar="<verb>")
    search = vault.add_parser("search", help="Full-text search of the active vault.")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)
    review = vault.add_parser("review", help="Vault-note review queue.").add_subparsers(dest="action", metavar="<action>")
    review.add_parser("list")
    show = review.add_parser("show"); show.add_argument("path")
    for verb in ("keep", "trash", "restore", "delete"):
        sub = review.add_parser(verb); sub.add_argument("--candidate", required=True)
        if verb == "delete":
            sub.add_argument("--confirm", required=True, help="Repeat the candidate id.")

    file_ = nouns.add_parser("file", help="Surface a workspace file in the pinned panel.").add_subparsers(dest="verb", metavar="<verb>")
    surface = file_.add_parser("surface"); surface.add_argument("path")

    chat = nouns.add_parser("chat", help="Chats in the active workspace.").add_subparsers(dest="verb", metavar="<verb>")
    lst = chat.add_parser("list"); lst.add_argument("--project", default="")
    for verb in ("get", "archive", "delete"):
        sub = chat.add_parser(verb); sub.add_argument("--chat", default="", help="Defaults to the calling chat.")
    create = chat.add_parser("create")
    create.add_argument("--project", default=None); create.add_argument("--title", default="New Chat")
    create.add_argument("--provider", default=None); create.add_argument("--model", default=None)
    create.add_argument("--mode", default=None); create.add_argument("--prompt", default=None)
    send = chat.add_parser("send"); send.add_argument("--chat", required=True); send.add_argument("--prompt", required=True)
    stop = chat.add_parser("stop"); stop.add_argument("--chat", required=True)

    schedule = nouns.add_parser("schedule", help="Schedules in the active workspace.").add_subparsers(dest="verb", metavar="<verb>")
    schedule.add_parser("list")
    for verb in ("create", "preview"):
        sub = schedule.add_parser(verb)
        for flag, _param, kind in _SCHEDULE_FLAGS:
            sub.add_argument(flag, default=None, type=(int if kind is int else str))
    upd = schedule.add_parser("update"); upd.add_argument("schedule_id")
    for flag, _param, kind in _SCHEDULE_FLAGS:
        upd.add_argument(flag, default=None, type=(int if kind is int else str))
    for verb in ("pause", "resume", "run", "delete"):
        sub = schedule.add_parser(verb); sub.add_argument("schedule_id")
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


def resolve(args: argparse.Namespace) -> tuple[str, dict[str, Any]] | None:
    """Map parsed arguments to ``(operation, arguments)``; ``None`` for help."""
    noun, verb = args.noun, getattr(args, "verb", None)
    if noun == "help":
        return None
    if noun == "context" and verb == "get":
        return "context_get", {}
    if noun == "memory":
        if verb == "status":
            return "memory_status", {}
        if verb == "update":
            return "memory_update", {"region": args.region, "action": args.action, "entry": args.entry, "match": args.match}
    if noun == "vault":
        if verb == "search":
            return "vault_search", {"query": args.query, "limit": args.limit}
        if verb == "review":
            action = args.action
            if action == "list":
                return "vault_review", {"action": "list"}
            if action == "show":
                return "vault_review", {"action": "inspect", "path": args.path}
            if action == "keep":
                return "vault_review", {"action": "decide", "candidate_id": args.candidate, "disposition": "keep"}
            if action in ("trash", "restore"):
                return "vault_review", {"action": action, "candidate_id": args.candidate}
            if action == "delete":
                return "vault_review", {"action": "delete", "candidate_id": args.candidate, "confirm": args.confirm}
    if noun == "file" and verb == "surface":
        return "file_surface", {"path": args.path}
    if noun == "chat":
        if verb == "list":
            return "chats_list", {"project_id": args.project}
        if verb in ("get", "archive", "delete"):
            return f"chat_{verb}", {"chat_id": args.chat}
        if verb == "create":
            payload = {k: getattr(args, k) for k in ("project", "title", "provider", "model", "mode", "prompt")}
            payload["project_id"] = payload.pop("project")
            return "chat_create", {k: v for k, v in payload.items() if v is not None}
        if verb == "send":
            return "chat_send", {"chat_id": args.chat, "prompt": args.prompt}
        if verb == "stop":
            return "chat_stop", {"chat_id": args.chat}
    if noun == "schedule":
        if verb == "list":
            return "schedules_list", {}
        if verb in ("create", "update", "preview"):
            return "schedule", _schedule_arguments(args, verb)
        if verb in ("pause", "resume", "run", "delete"):
            return "schedule_action", {"schedule_id": args.schedule_id, "action": verb}
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
    args = parser.parse_args(argv)
    if args.noun is None:
        parser.print_help()
        return 2
    if args.noun == "help":
        try:
            sys.stdout.write(_SKILL_PATH.read_text(encoding="utf-8"))
        except OSError:
            parser.print_help()
        return 0
    resolved = resolve(args)
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
