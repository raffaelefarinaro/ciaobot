"""Dev-only entry point for the agentic task eval: ``python -m evals <list|run|report|compare>``.

Not part of the wheel. Docs: ``docs/TASK_EVAL.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _tasks_command(args: argparse.Namespace) -> int:
    """``python -m evals``: the agentic task eval (catalog, isolated runs, report, compare)."""
    from functools import partial

    from evals import task_eval

    action = getattr(args, "tasks_action", "")
    if action == "list":
        tasks = task_eval.load_tasks()
        rows = [
            {
                "id": task.id,
                "tier": task.tier,
                "category": task.category,
                "entry": task.entry["kind"],
                "requires": task.requires,
            }
            for task in tasks
        ]
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            for row in rows:
                print(f"{row['id']}\t{row['tier']}\t{row['category']}\t{','.join(row['requires']) or '-'}")
        return 0
    if action == "run":
        mismatch = task_eval.cli_package_mismatch()
        if mismatch:
            print(mismatch, file=sys.stderr)
            return 2
        tiers = {item.strip() for item in args.tier.split(",") if item.strip()}
        include = {item.strip() for item in args.include.split(",") if item.strip()}
        selected = [
            task
            for task in task_eval.load_tasks()
            if (not tiers or task.tier in tiers) and (not include or task.id in include)
        ]
        if not selected:
            print("no tasks selected", file=sys.stderr)
            return 2
        out = args.out or task_eval.default_report_path(args.label)
        rows = task_eval.run_suite(
            selected,
            provider=args.provider,
            model=args.model,
            label=args.label,
            repeats=args.repeats,
            jobs=args.jobs,
            out=out,
            mode=args.mode,
            session_factory=partial(
                task_eval.IsolatedEngine.boot,
                provider=args.provider,
                model=args.model,
                keep_workdir=args.keep_workdir,
                mode=args.mode,
            ),
            capabilities=task_eval.available_capabilities(),
        )
        print(task_eval.render_summary(task_eval.summarize(rows)))
        print(f"wrote {len(rows)} run(s) to {out}")
        return 1 if rows and all(row["status"] == "error" for row in rows) else 0
    if action == "report":
        summary = task_eval.summarize(task_eval.read_jsonl(args.files))
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(task_eval.render_summary(summary))
        return 0
    if action == "compare":
        allow_mixed = bool(getattr(args, "allow_mixed", False))
        try:
            comparison = task_eval.compare(
                task_eval.read_jsonl([args.baseline]),
                task_eval.read_jsonl([args.candidate]),
                allow_mixed=allow_mixed,
            )
        except task_eval.MixedRunsError as exc:
            raise SystemExit(f"error: {exc}") from exc
        mixed = task_eval.mixed_runs(comparison)
        if mixed:
            print(
                "warning: --allow-mixed pooled runs of different providers or models:\n  "
                + "\n  ".join(mixed),
                file=sys.stderr,
            )
        if args.json:
            print(json.dumps(comparison, indent=2, ensure_ascii=False))
        else:
            print(task_eval.render_comparison(comparison))
        return 0
    raise SystemExit(f"unknown eval tasks action: {action!r}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description=(
            "Agentic task eval: boot an isolated engine per run, let Ciaobot act on a "
            "synthetic vault, grade the workspace, and record usage. 'run' makes real model calls."
        ),
    )
    tasks_sub = parser.add_subparsers(dest="tasks_action", required=True)
    tasks_list = tasks_sub.add_parser("list", help="List the task catalog.")
    tasks_list.add_argument("--json", action="store_true", help="Emit the catalog as JSON.")
    tasks_list.set_defaults(func=_tasks_command)

    tasks_run = tasks_sub.add_parser(
        "run",
        help="Run catalog tasks on isolated engines (explicit; makes real model calls).",
    )
    tasks_run.add_argument("--provider", default="claude", choices=["claude", "opencode"])
    tasks_run.add_argument(
        "--model",
        required=True,
        help="Provider model id to run. Required; the runner never defaults it.",
    )
    tasks_run.add_argument(
        "--label", default="candidate", help="Run label recorded on every row."
    )
    tasks_run.add_argument(
        "--repeats", type=int, default=3, help="Runs per task; the summary shows medians."
    )
    tasks_run.add_argument(
        "--jobs", type=int, default=1, help="Engines booted concurrently (each is a full engine)."
    )
    tasks_run.add_argument(
        "--include", default="", help="Comma-separated task ids (default: the whole catalog)."
    )
    tasks_run.add_argument(
        "--tier", default="", help="Comma-separated tiers: easy, medium, hard (default: all)."
    )
    from ciao.config import PERMISSION_MODES  # noqa: PLC0415

    tasks_run.add_argument(
        "--mode", choices=PERMISSION_MODES, default=None,
        help=(
            "Permission mode the run's chats start in (default per provider: "
            "claude auto, opencode bypass). Recorded on every row."
        ),
    )
    tasks_run.add_argument(
        "--out", type=Path, default=None,
        help="JSONL report path (default: .runtime/evals/tasks/<stamp>-<label>.jsonl).",
    )
    tasks_run.add_argument(
        "--keep-workdir", action="store_true",
        help="Keep each run's throwaway workspace on disk for debugging.",
    )
    tasks_run.set_defaults(func=_tasks_command)

    tasks_report = tasks_sub.add_parser(
        "report", help="Summarize one or more task-eval JSONL reports."
    )
    tasks_report.add_argument("files", nargs="+", type=Path)
    tasks_report.add_argument("--json", action="store_true", help="Emit the summary as JSON.")
    tasks_report.set_defaults(func=_tasks_command)

    tasks_compare = tasks_sub.add_parser(
        "compare", help="Compare a baseline and a candidate task-eval JSONL report."
    )
    tasks_compare.add_argument("--baseline", type=Path, required=True)
    tasks_compare.add_argument("--candidate", type=Path, required=True)
    tasks_compare.add_argument(
        "--allow-mixed",
        action="store_true",
        help="Pool a task's runs even when one side mixes providers or models (warns on stderr).",
    )
    tasks_compare.add_argument(
        "--json", action="store_true", help="Emit the comparison as JSON."
    )
    tasks_compare.set_defaults(func=_tasks_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
