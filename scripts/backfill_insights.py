#!/usr/bin/env python3
"""Backfill `## Session insights` sections into existing archived chats.

Live archives get insights appended automatically (see ``ciao/insights.py``
+ the ``chat_archive`` route). This one-shot script does the same for
chats archived before the feature existed, walking
``memory-vault/Logs/Chats/<context>/claude/*.md``.

Two modes per archive, picked automatically:

* **Full mode** when the source JSONL still exists at
  ``~/.claude/projects/-home-ubuntu-ciao/<session-id>.jsonl``. Same
  pre-filter as the live flow (truncates Read/Glob/Grep, keeps
  Edit/Write/Bash/Task and errors in full).
* **Text fallback** when the JSONL has been deleted. The rendered
  markdown body is fed to the model with a prompt variant that drops
  the idx-citation requirement and warns the model that tool calls,
  errors, and vault edits aren't visible. Sections like Errors and
  Reusable snippets will typically come back empty in this mode -
  that's correct, the model should omit them.

Idempotent: archives that already contain ``## Session insights`` are
skipped, so re-running is safe.

Usage::

    .venv/bin/python scripts/backfill_insights.py --dry-run
    .venv/bin/python scripts/backfill_insights.py --limit 3
    .venv/bin/python scripts/backfill_insights.py --mode full
    .venv/bin/python scripts/backfill_insights.py --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure repo root on sys.path so `ciao.*` imports work when the script is
# run directly. The script lives at scripts/backfill_insights.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ciao import insights  # noqa: E402
from ciao.config import CiaoConfig  # noqa: E402

logger = logging.getLogger("backfill_insights")

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

_TEXT_MODE_SYSTEM_PROMPT = """\
You are extracting durable signal from a Claude Code chat transcript.
The user is the workspace owner. The transcript is a rendered Markdown summary -
tool calls, tool errors, thinking blocks, and intermediate states are
NOT included, only the user/assistant text turns. Adjust accordingly:
sections like Errors, Reusable snippets, and Vault changes will often
be empty. Omit empty sections - do NOT write "none" or "n/a".

Cite by short paraphrase or quote (no `[idx=N]` indices in this mode).
Do not invent facts. Do not summarise the conversation - that is the
transcript itself.

Rules:
- Skip anything obvious from the transcript prose alone.
- "User corrections" = the user pushed back, redirected, or rejected an approach.
- "New entities" = people/projects/places/products mentioned for the first time.
- Be terse. One line per item where possible.

## User corrections
- User said: "<short quote>" -> assistant changed <what>.

## New entities
- <type>: <name> - <one-line context>.

## Decisions
- Chose <X> over <Y> because <reason>.

## Open loops
- <thing left undone, with any deadline or condition>.

## Errors
- <if the transcript itself describes a failure resolution that's worth keeping>

## Reusable snippets
- <only if a fully formed command or query appears in the assistant text>
"""


@dataclass(slots=True)
class Archive:
    path: Path
    context: str
    session_id: str | None
    has_jsonl: bool
    already_done: bool


def _discover_archives(vault_root: Path) -> list[Archive]:
    """Walk memory-vault/Logs/Chats/<ctx>/claude/*.md."""
    base = vault_root / "memory-vault" / "Logs" / "Chats"
    if not base.exists():
        return []
    archives: list[Archive] = []
    workspace_root = vault_root  # Same as repo root in our layout.
    project_dir = (
        Path.home() / ".claude" / "projects"
        / f"-{str(workspace_root).replace('/', '-').lstrip('-')}"
    )
    for md in sorted(base.glob("*/claude/*.md")):
        match = UUID_RE.search(md.name)
        session_id = match.group(0) if match else None
        has_jsonl = bool(
            session_id and (project_dir / f"{session_id}.jsonl").exists()
        )
        try:
            already_done = "## Session insights" in md.read_text(encoding="utf-8")
        except OSError:
            already_done = False
        # Context = the chat-XXX folder name above /claude/.
        context = md.parent.parent.name
        archives.append(
            Archive(
                path=md,
                context=context,
                session_id=session_id,
                has_jsonl=has_jsonl,
                already_done=already_done,
            )
        )
    return archives


async def _process_full(
    archive: Archive,
    *,
    config: CiaoConfig,
) -> tuple[str, str]:
    """Full-mode extraction using JSONL pre-filter + standard prompt."""
    assert archive.session_id is not None
    filtered = insights.filter_session_jsonl(
        config.workspace_root, archive.session_id
    )
    if not filtered:
        return "skipped", "filter returned empty"
    await insights.extract_and_append(
        archive_path=archive.path,
        filtered_jsonl=filtered,
        ollama_settings=config.ollama,
        model=config.insights_model,
    )
    if "## Session insights" in archive.path.read_text(encoding="utf-8"):
        return "ok", "full"
    return "skipped", "extract returned empty"


async def _process_text(
    archive: Archive,
    *,
    config: CiaoConfig,
) -> tuple[str, str]:
    """Text-mode fallback: feed the rendered markdown body to the model."""
    body = archive.path.read_text(encoding="utf-8")
    # Strip any frontmatter-ish leading metadata lines before sending.
    user_prompt = (
        "Below is a rendered Markdown chat transcript. Tool calls, errors, "
        "and thinking blocks are not preserved - only user/assistant text. "
        "Extract durable signal per the system prompt's section schema.\n\n"
        f"{body}"
    )
    env = insights._ollama_env(config.ollama)
    output = await _call_with_retry(
        user_prompt=user_prompt,
        system_prompt=_TEXT_MODE_SYSTEM_PROMPT,
        model=config.insights_model,
        env=env,
    )
    if not output:
        return "skipped", "model returned empty"
    insights._append_section(archive.path, output)
    return "ok", "text"


async def _call_with_retry(
    *,
    user_prompt: str,
    system_prompt: str,
    model: str,
    env: dict[str, str],
) -> str:
    """Like insights._run_model_with_retry but with a custom system prompt."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        TextBlock,
        query,
    )

    async def _once() -> str:
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            setting_sources=[],
            skills=[],
            tools=[],
            strict_mcp_config=True,
            max_turns=1,
            env=env,
        )
        parts: list[str] = []
        async for msg in query(prompt=user_prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
        return "".join(parts).strip()

    try:
        return await _once()
    except Exception as exc:  # noqa: BLE001
        logger.info("model call failed (%s); retrying in 30s", exc)
    await asyncio.sleep(30)
    try:
        return await _once()
    except Exception:
        logger.exception("model call failed twice; skipping")
        return ""


async def _worker(
    sem: asyncio.Semaphore,
    archive: Archive,
    *,
    config: CiaoConfig,
    mode_filter: str,
) -> tuple[Archive, str, str]:
    async with sem:
        if archive.already_done:
            return archive, "skipped", "already has insights"
        if archive.session_id is None:
            return archive, "skipped", "no session_id in filename"
        try:
            if archive.has_jsonl and mode_filter in {"both", "full"}:
                status, detail = await _process_full(archive, config=config)
            elif (not archive.has_jsonl) and mode_filter in {"both", "text"}:
                status, detail = await _process_text(archive, config=config)
            else:
                return archive, "skipped", f"mode={mode_filter} excludes this archive"
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker failed on %s", archive.path)
            return archive, "errored", str(exc)
        return archive, status, detail


def _format_path(p: Path) -> str:
    """Display path as relative to repo root for readable logs."""
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


async def main_async(args: argparse.Namespace) -> int:
    config = CiaoConfig.from_env()
    archives = _discover_archives(REPO_ROOT)
    if args.workspace:
        archives = [a for a in archives if a.context == args.workspace]

    total = len(archives)
    todo = [
        a for a in archives
        if not a.already_done and a.session_id is not None
        and (
            (a.has_jsonl and args.mode in {"both", "full"})
            or ((not a.has_jsonl) and args.mode in {"both", "text"})
        )
    ]
    if args.limit:
        todo = todo[: args.limit]

    print(f"discovered: {total} archives")
    print(f"  already have insights: {sum(1 for a in archives if a.already_done)}")
    print(f"  full mode (jsonl present): {sum(1 for a in archives if a.has_jsonl and not a.already_done)}")
    print(f"  text mode (jsonl missing): {sum(1 for a in archives if not a.has_jsonl and a.session_id and not a.already_done)}")
    print(f"  no session_id (skipped): {sum(1 for a in archives if a.session_id is None)}")
    print(f"will process: {len(todo)} (limit={args.limit or 'none'}, mode={args.mode})")

    if args.dry_run:
        for a in todo[:20]:
            mode = "full" if a.has_jsonl else "text"
            print(f"  [{mode}] {_format_path(a.path)}")
        if len(todo) > 20:
            print(f"  ... and {len(todo) - 20} more")
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    tasks = [
        _worker(sem, a, config=config, mode_filter=args.mode)
        for a in todo
    ]
    counters = {"ok-full": 0, "ok-text": 0, "skipped": 0, "errored": 0}
    for fut in asyncio.as_completed(tasks):
        archive, status, detail = await fut
        if status == "ok":
            key = f"ok-{detail}"
            counters[key] = counters.get(key, 0) + 1
        else:
            counters[status] = counters.get(status, 0) + 1
        print(f"  [{status:8s}] {_format_path(archive.path)} ({detail})")
    print()
    print("summary:")
    for k, v in sorted(counters.items()):
        print(f"  {k}: {v}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be processed without calling the model.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N archives (0 = no limit).",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default="",
        help="Only archives under this context (e.g. chat-3e2df9e3).",
    )
    parser.add_argument(
        "--mode",
        choices=("both", "full", "text"),
        default="both",
        help="Which extraction mode to run (default: both).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Parallel model calls. Keep low to be gentle on Ollama (default 2).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
