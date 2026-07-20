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
import sys
from pathlib import Path

# Ensure repo root on sys.path so `ciao.*` imports work when the script is
# run directly.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ciao.config import CiaoConfig
from ciao.insights import backfill_insights_task


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
    config = CiaoConfig.from_env()
    asyncio.run(
        backfill_insights_task(
            config,
            limit=args.limit,
            mode=args.mode,
            dry_run=args.dry_run,
            concurrency=args.concurrency,
            workspace=args.workspace,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
