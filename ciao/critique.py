"""Multi-model adversarial review via the Claude Agent SDK.

Replaces the former Pi-subprocess panel. Each model in the panel is called
through :func:`ciao.providers.oneshot.run_oneshot` with per-model routing
env (OpenRouter / Ollama / Anthropic), so ``owner/model`` ids reach
OpenRouter, ``:tag`` ids reach Ollama, and bare aliases stay on Anthropic.
The artifact is inlined in the prompt (the one-shot call runs with no
tools, ``max_turns=1``), so no file-read tool is needed.

Exposed as a CLI (``python -m ciao.critique``) and, via :func:`run_panel`, as
the ``adversarial_review`` MCP tool (``ciao/control_plane.py``) — both call
the same panel-running logic so they can't drift.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from ciao.config import CiaoConfig
from ciao.providers.oneshot import run_oneshot
from ciao.providers.routing import routing_routine_env_for_model

DEFAULT_TIMEOUT = 120  # seconds per model

# Panel entries prefixed with this route through the Codex (OpenAI / ChatGPT)
# app-server instead of the Anthropic-compatible one-shot path.
CODEX_PREFIX = "codex:"

_CODEX_AVAILABLE_CACHE: tuple[float, bool] | None = None
_CODEX_AVAILABLE_TTL = 30.0  # seconds — panel resolution is read-hot

def is_anthropic_available() -> bool:
    """True when Anthropic API key or Claude Code OAuth credentials are present."""
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return True
    for p in [
        Path.home() / ".claude-agent" / "credentials.json",
        Path.home() / ".claude" / ".credentials.json",
    ]:
        try:
            if p.is_file():
                return True
        except Exception:
            pass
    raw_cfg = os.environ.get("CLAUDE_CONFIG_PATH", "").strip()
    config_path = Path(raw_cfg).expanduser() if raw_cfg else Path.home() / ".claude.json"
    try:
        if config_path.is_file():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("oauthAccount"):
                return True
    except Exception:
        pass
    return False


def is_codex_available() -> bool:
    """True when the Codex CLI is installed and signed in (OpenAI / ChatGPT).

    Backs the "add the OpenAI voice to the critique panel" default. Result is
    cached briefly because :func:`default_critique_panel` runs on every
    Settings → Models read and ``codex login status`` spawns a subprocess;
    when no Codex binary is present the check returns immediately without one.
    """
    global _CODEX_AVAILABLE_CACHE
    now = time.monotonic()
    cached = _CODEX_AVAILABLE_CACHE
    if cached is not None and now - cached[0] < _CODEX_AVAILABLE_TTL:
        return cached[1]
    try:
        from ciao.providers.codex import codex_login_status

        ok = bool(codex_login_status().get("ok"))
    except Exception:  # noqa: BLE001 — availability probe must never crash the panel
        ok = False
    _CODEX_AVAILABLE_CACHE = (now, ok)
    return ok


def default_critique_panel(config: CiaoConfig) -> list[str]:
    """Backend-aware default when Settings → Models has no critique override."""
    models = []

    # Prioritize native Anthropic over OpenRouter for the Claude models
    if is_anthropic_available():
        models.extend(["opus", "fable"])
    elif config.openrouter.available:
        models.extend([config.openrouter.opus_model, config.openrouter.fable_model])
    else:
        # Fallback when neither is explicitly configured
        models.extend(["opus", "fable"])

    # Add Ollama models if Ollama is configured / available
    oll = config.ollama
    if bool(oll.local_models) or (bool(oll.api_key) and oll.api_key != "ollama"):
        if oll.opus_model:
            models.append(oll.opus_model)
        if oll.fable_model:
            models.append(oll.fable_model)

    # Add the OpenAI (Codex) fable model when the Codex CLI is signed in, so a
    # ChatGPT / OpenAI account lends a non-Anthropic voice to the panel. The
    # ``codex:`` prefix routes the entry through the Codex app-server; the bare
    # ``fable`` tier resolves to the signed-in account's model at dispatch.
    if is_codex_available():
        models.append(f"{CODEX_PREFIX}fable")

    # Filter out empty strings or duplicates while preserving order
    seen = set()
    unique_models = []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            unique_models.append(m)

    return unique_models


def resolve_critique_panel(config: CiaoConfig, *, override: str = "") -> list[str]:
    """Resolve the adversarial-review panel from UI/env overrides or defaults."""
    csv = override.strip() or (config.critique_models or "").strip()
    if csv:
        return [model.strip() for model in csv.split(",") if model.strip()]
    return default_critique_panel(config)


def critique_models_effective(config: CiaoConfig) -> str:
    """Comma-separated panel string for API responses."""
    return ",".join(resolve_critique_panel(config))


def apply_app_settings_overlay(config: CiaoConfig) -> None:
    """Overlay `.runtime/app_settings.json` onto ``config`` (Settings → Models)."""
    from ciao.app_settings import AppSettingsStore

    runtime_env = os.environ.get("CIAO_RUNTIME_ROOT", os.environ.get("TELEGRAM_BRIDGE_RUNTIME_ROOT", "")).strip()
    runtime_root = Path(runtime_env) if runtime_env else config.state_path.parent
    AppSettingsStore(runtime_root / "app_settings.json").apply_to_config(config)


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
        parsed: dict = json.loads(text)
        return parsed
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
                    parsed_blob: dict = json.loads(blob)
                    return parsed_blob
                except Exception:
                    return None
    return None


def _split_provider(model: str) -> tuple[str, str]:
    """Map a panel entry to its ``(provider, model_id)`` dispatch pair.

    A ``codex:`` prefix selects the Codex (OpenAI / ChatGPT) app-server; every
    other id runs through the Anthropic-compatible one-shot path with per-model
    routing env (Anthropic passthrough / OpenRouter / Ollama).
    """
    if model.startswith(CODEX_PREFIX):
        return "codex", model[len(CODEX_PREFIX):].strip()
    return "claude", model


async def _review_one(
    model: str, artifact: str, user_prompt: str, config: CiaoConfig, timeout: float
) -> ModelResult:
    provider, model_id = _split_provider(model)
    # Codex routes by provider, not by env injection; the Anthropic-compatible
    # path picks its upstream from the model id shape.
    env = {} if provider == "codex" else routing_routine_env_for_model(model_id, config)
    t0 = time.monotonic()
    try:
        raw = await run_oneshot(
            user_prompt,
            system_prompt=SYSTEM_PROMPT,
            model=model_id,
            env=env,
            timeout_s=timeout,
            provider=provider,
            cwd=config.workspace_root if provider == "codex" else None,
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


async def run_panel(
    panel: list[str],
    artifact: str,
    user_prompt: str,
    config: CiaoConfig,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_parallel: int = 8,
) -> list[ModelResult]:
    """Run every model in ``panel`` concurrently and return results in panel order.

    Shared by the CLI (``async_main``) and the ``adversarial_review`` MCP tool
    so the two entrypoints can't drift on how the panel is actually run.
    """
    sem = asyncio.Semaphore(max_parallel)

    async def _run(model: str) -> ModelResult:
        async with sem:
            return await _review_one(model, artifact, user_prompt, config, timeout)

    results = await asyncio.gather(*[_run(m) for m in panel])
    order = {m: i for i, m in enumerate(panel)}
    return sorted(results, key=lambda r: order.get(r.model, 999))


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
        review = r.review
        if review is None:
            continue
        v = review.get("verdict", "?")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    severity_weight = {"blocking": 4, "major": 3, "minor": 2, "nit": 1}
    issues: list[dict] = []
    for r in ok:
        review = r.review
        if review is None:
            continue
        for it in review.get("issues") or []:
            issues.append({**it, "model": r.model, "confidence": review.get("confidence", 3)})
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
    apply_app_settings_overlay(config)

    models = resolve_critique_panel(
        config,
        override=(
            args.models
            or os.environ.get("CIAO_REVIEW_MODELS", "")
            or os.environ.get("CIAO_ADVERSARIAL_MODELS", "")
        ),
    )
    if not models:
        print("error: no critique models configured (set panel in Settings → Models)", file=sys.stderr)
        return 2

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
    results = await run_panel(
        models, artifact, user_prompt, config,
        timeout=args.timeout, max_parallel=args.max_parallel,
    )
    for r in results:
        status = "OK" if r.ok else f"FAIL ({r.error})"
        print(f"[adversarial-review] {r.model}: {status} in {r.elapsed_s:.1f}s", file=sys.stderr)

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
