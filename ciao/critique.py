"""Multi-model adversarial review via the Claude Agent SDK.

Replaces the former Pi-subprocess panel. Each model in the panel is called
through :func:`ciao.providers.oneshot.run_oneshot` with per-model routing
env (OpenRouter / Ollama / Anthropic), so ``owner/model`` ids reach
OpenRouter, ``:tag`` ids reach Ollama, and bare aliases stay on Anthropic.
The artifact is inlined in the prompt (the one-shot call runs with no
tools, ``max_turns=1``), so no file-read tool is needed.

Exposed as a CLI (``python -m ciao.critique``) with the same flags the old
``skills/adversarial-review/scripts/review.py`` accepted, so the skill's
``$SKILL_DIR/scripts/review.py`` can thin-wrap it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from ciao.config import CiaoConfig
from ciao.providers.oneshot import run_oneshot
from ciao.providers.routing import routing_routine_env_for_model

DEFAULT_PANEL = [
    "anthropic/claude-sonnet-latest",
    "anthropic/claude-haiku-latest",
    "anthropic/claude-opus-latest",
]
DEFAULT_TIMEOUT = 120  # seconds per model


SYSTEM_PROMPT = """You are an adversarial reviewer. The user is about to ship the artifact below and wants you to find what's wrong with it before anyone else does. Be sharp, specific, and honest. Don't hedge, don't flatter, and don't pad with generic best-practice advice that doesn't apply to this artifact.

Rules:
- Read the artifact end-to-end before responding.
- Surface concrete problems tied to specific passages, sections, or claims. Quote when useful.
- Prefer one strong critique over five weak ones. If something is fine, don't critique it.
- Distinguish: blocking (must fix before shipping), major (should fix), minor (nice to fix), nit (style).
- Note hidden assumptions, missing edge cases, internal contradictions, weak evidence, unstated trade-offs, and audience mismatches.
- If you actually think the artifact is solid, say so plainly and only flag the few things worth flagging.

Respond with a single JSON object, no prose around it, matching this schema:
{
  "verdict": "ship" | "revise" | "block",
  "confidence": 1-5,
  "summary": "<=2 sentences on the overall state of the artifact",
  "strengths": ["..."],
  "issues": [
    {"severity": "blocking|major|minor|nit", "where": "section/quote/locator", "claim": "what's wrong", "why": "why it matters", "suggestion": "concrete fix"}
  ],
  "missing": ["things the artifact should address but doesn't"],
  "questions_for_author": ["sharp questions whose answers would change your verdict"]
}
"""

USER_PROMPT_TEMPLATE = """Artifact type: {doc_type}
{focus_block}{context_block}
Artifact:
```
{artifact}
```

Now produce your JSON review."""


@dataclass
class ModelResult:
    model: str
    elapsed_s: float
    ok: bool
    review: dict[str, Any] | None = None
    raw_text: str | None = None
    error: str | None = None


def extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    return json.loads(blob)
                except Exception:
                    return None
    return None


async def _review_one(
    model: str, artifact: str, user_prompt: str, config: CiaoConfig, timeout: float
) -> ModelResult:
    import time

    env = routing_routine_env_for_model(model, config)
    t0 = time.monotonic()
    try:
        raw = await run_oneshot(
            user_prompt,
            system_prompt=SYSTEM_PROMPT,
            model=model,
            env=env,
            timeout_s=timeout,
        )
    except (TimeoutError, OSError, RuntimeError) as exc:
        return ModelResult(model, time.monotonic() - t0, False, error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — never crash the panel
        return ModelResult(model, time.monotonic() - t0, False, error=f"{type(exc).__name__}: {exc}")
    elapsed = time.monotonic() - t0
    raw = (raw or "").strip()
    parsed = extract_json(raw)
    if parsed is None:
        return ModelResult(model, elapsed, False, raw_text=raw[:2000], error="Could not parse JSON")
    return ModelResult(model, elapsed, True, review=parsed, raw_text=raw)


def _group_count(items: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        v = it.get(key, "?")
        out[v] = out.get(v, 0) + 1
    return out


def aggregate(results: list[ModelResult]) -> dict:
    ok = [r for r in results if r.ok and r.review]
    verdict_counts: dict[str, int] = {}
    for r in ok:
        v = r.review.get("verdict", "?")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    severity_weight = {"blocking": 4, "major": 3, "minor": 2, "nit": 1}
    issues: list[dict] = []
    for r in ok:
        for it in r.review.get("issues") or []:
            issues.append({**it, "model": r.model, "confidence": r.review.get("confidence", 3)})
    return {
        "model_count": len(results),
        "ok_count": len(ok),
        "verdicts": verdict_counts,
        "total_issues": len(issues),
        "by_severity": _group_count(issues, "severity"),
        "issues": sorted(
            issues,
            key=lambda x: (-severity_weight.get(x.get("severity", "minor"), 0), -x.get("confidence", 3)),
        ),
    }


def render_markdown(artifact_name: str, results: list[ModelResult], agg: dict) -> str:
    out: list[str] = [f"# Adversarial review: {artifact_name}\n"]
    out.append(f"**Panel:** {len(results)} models, {agg['ok_count']} responded successfully.\n")
    if agg["verdicts"]:
        out.append("**Verdicts:** " + ", ".join(f"{k}={v}" for k, v in sorted(agg["verdicts"].items(), key=lambda x: -x[1])))
    if agg["by_severity"]:
        out.append("**Issues by severity:** " + ", ".join(f"{k}={v}" for k, v in sorted(agg["by_severity"].items(), key=lambda x: -x[1])) + "\n")
    out.append("")
    failures = [r for r in results if not r.ok]
    if failures:
        out.append("## Failed models")
        for r in failures:
            out.append(f"- `{r.model}` ({r.elapsed_s:.1f}s): {r.error}")
        out.append("")
    out.append("## Per-model verdicts\n")
    for r in results:
        if not r.ok or not r.review:
            continue
        rv = r.review
        out.append(f"### `{r.model}`  — verdict: **{rv.get('verdict', '?')}**, confidence: {rv.get('confidence', '?')}/5  ({r.elapsed_s:.1f}s)")
        if rv.get("summary"):
            out.append(f"\n_{rv['summary']}_\n")
        issues = rv.get("issues") or []
        if issues:
            out.append("**Issues:**")
            for it in issues:
                sev = it.get("severity", "?")
                where = it.get("where", "")
                where_s = f" _({where})_" if where else ""
                out.append(f"- **[{sev}]** {it.get('claim', '')}{where_s}")
                if it.get("why"):
                    out.append(f"  - why: {it['why']}")
                if it.get("suggestion"):
                    out.append(f"  - fix: {it['suggestion']}")
        for label, key in (("Missing", "missing"), ("Questions", "questions_for_author"), ("Strengths", "strengths")):
            items = rv.get(key) or []
            if items:
                out.append(f"**{label}:**")
                for m in items:
                    out.append(f"- {m}")
        out.append("")
    return "\n".join(out)


def _load_app_settings_models() -> str:
    """Read the critique_models override from .runtime/app_settings.json."""
    runtime_env = os.environ.get("CIAO_RUNTIME_ROOT", os.environ.get("TELEGRAM_BRIDGE_RUNTIME_ROOT", "")).strip()
    repo_root = Path(__file__).resolve().parents[1]
    runtime_root = Path(runtime_env) if runtime_env else repo_root / ".runtime"
    path = runtime_root / "app_settings.json"
    if not path.is_file():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("critique_models", "").strip()
    except (OSError, ValueError):
        return ""


async def async_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Adversarial multi-model review via the Claude Agent SDK.")
    p.add_argument("--input", help="Path to the artifact file. If omitted, reads from stdin.")
    p.add_argument("--type", default="document", help="Artifact type (spec, prd, plan, brief, doc, email, code).")
    p.add_argument("--focus", default="", help="Optional focus area.")
    p.add_argument("--context", default="", help="Optional extra context for the reviewers.")
    p.add_argument("--models", default="", help="Comma-separated model ids. Overrides defaults/env.")
    p.add_argument("--print-panel", action="store_true", help="Resolve and print the panel, then exit.")
    p.add_argument("--format", choices=["markdown", "json"], default="markdown")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Per-model timeout in seconds.")
    p.add_argument("--max-parallel", type=int, default=8, help="Max concurrent model calls.")
    args = p.parse_args(argv)

    config = CiaoConfig.from_env()

    models_csv = (
        args.models
        or _load_app_settings_models()
        or os.environ.get("CIAO_REVIEW_MODELS", "")
        or os.environ.get("CIAO_ADVERSARIAL_MODELS", "")
    )
    if models_csv:
        models = [m.strip() for m in models_csv.split(",") if m.strip()]
    else:
        # Dynamic default: OpenRouter panel when a key is set, else Ollama tiers.
        if config.openrouter.available:
            models = list(DEFAULT_PANEL)
        elif config.ollama.api_key and config.ollama.api_key != "ollama":
            models = [config.ollama.sonnet_model, config.ollama.haiku_model, config.ollama.opus_model]
        else:
            models = ["sonnet", "haiku", "opus"]  # Anthropic aliases

    if args.print_panel:
        for m in models:
            print(m)
        return 0

    if args.input:
        artifact_path = Path(args.input).expanduser().resolve()
        artifact_name = artifact_path.name
        if not artifact_path.exists():
            print(f"error: artifact not found: {artifact_path}", file=sys.stderr)
            return 2
        artifact = artifact_path.read_text(encoding="utf-8", errors="replace")
    else:
        artifact = sys.stdin.read()
        artifact_name = "stdin"
        if not artifact.strip():
            print("error: empty artifact", file=sys.stderr)
            return 2

    focus_block = f"Focus area: {args.focus}\n" if args.focus else ""
    context_block = f"Author context: {args.context}\n" if args.context else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        doc_type=args.type, focus_block=focus_block, context_block=context_block, artifact=artifact
    )

    print(f"[adversarial-review] panel: {', '.join(models)}", file=sys.stderr)
    sem = asyncio.Semaphore(args.max_parallel)

    async def _run(model: str) -> ModelResult:
        async with sem:
            return await _review_one(model, artifact, user_prompt, config, args.timeout)

    results = await asyncio.gather(*[_run(m) for m in models])
    for r in results:
        status = "OK" if r.ok else f"FAIL ({r.error})"
        print(f"[adversarial-review] {r.model}: {status} in {r.elapsed_s:.1f}s", file=sys.stderr)

    order = {m: i for i, m in enumerate(models)}
    results = sorted(results, key=lambda r: order.get(r.model, 999))
    agg = aggregate(results)

    if args.format == "json":
        print(json.dumps({"artifact": artifact_name, "type": args.type, "focus": args.focus, "aggregate": agg, "results": [asdict(r) for r in results]}, indent=2))
    else:
        print(render_markdown(artifact_name, results, agg))
    return 0 if agg["ok_count"] > 0 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
