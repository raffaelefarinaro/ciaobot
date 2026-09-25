"""Dry-run comparison: one-shot insights extraction against agent-mode.

Question #586 asks: does giving the extractor the vault (Read/Grep/Glob
inside the workspace vault) produce better insights than pasting it a
roster? The only honest way to answer is to run both over the same real
archives and let a human read the two outputs side by side.

So this module is deliberately inert. It selects archived chats, runs each
through both modes, routes both outputs through the same pure
:func:`ciao.memory_proposals.route_insights`, and writes a Markdown report
into ``Workspace/``. Nothing is applied: no proposals file, no region
writes, no archive edits. A per-chat JSON cache under the runtime root makes
an interrupted run resume instead of re-paying for finished chats.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import statistics
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# An archive shorter than this is a stub, not a session: both modes would
# return nothing worth reading and the call would cost the same as a real
# one. The same 800-character floor the rest of the codebase treats as a
# non-empty transcript.
_MIN_ARCHIVE_CHARS = 800

# The `varied` sampler draws from the newest N before splitting into
# terciles, so a single very long tail cannot crowd out short chats.
_VARIED_POOL_MULTIPLIER = 4

# Targets the report's per-destination kept counts, in the order the vault
# presents them.
_REPORT_TARGETS = (
    "memory",
    "profile",
    "project",
    "people",
    "learnings",
    "review",
)

# Providers whose read-only ruleset exists. A chat on anything else records
# `skipped: provider` rather than pretending the comparison covered it.
_AGENT_PROVIDERS = ("claude", "opencode")


@dataclass
class Candidate:
    """One archived chat the comparison may run over."""

    archive_path: Path
    chat_id: str
    provider: str
    workspace: str
    chars: int


@dataclass
class ModeResult:
    """What one mode produced for one chat."""

    mode: str
    status: str  # ok | error | skipped: provider
    seconds: float
    output: str
    kept: list[dict]
    suppressed: int
    dropped: int
    turns: int = 0
    tool_calls: int = 0
    cost_usd: float | None = None
    denied: int = 0
    error: str = ""


def strip_insights(text: str) -> str:
    """An archive body with its appended ``## Session insights`` section removed.

    Both modes are being asked the same question about the same chat, so
    neither may see the previous extraction's answer. A body with no appended
    section is returned unchanged — the caller is re-running, not sanitising.
    """
    from ciao.insights import locate_insights_section

    location = locate_insights_section(text)
    if location is None:
        return text
    return text[: location[0]]


def _chat_workspaces(runtime_root: Path) -> dict[str, str]:
    """Chat id → workspace, from the PWA chat registry.

    An archive's path names the chat that wrote it, not the workspace it ran
    in, so the only place that mapping exists is `web_projects.json`. An
    absent or unreadable store filters nothing, which the caller sees as an
    empty mapping rather than an error: a workspace-scoped run over a vault
    with no chat registry legitimately has nothing to compare.
    """
    path = runtime_root / "web_projects.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    chats = data.get("chats")
    projects = data.get("projects")
    if not isinstance(chats, dict) or not isinstance(projects, dict):
        return {}
    out: dict[str, str] = {}
    for chat_id, chat in chats.items():
        if not isinstance(chat, dict):
            continue
        project = projects.get(str(chat.get("project_id") or ""))
        if isinstance(project, dict):
            workspace = str(project.get("workspace") or "")
            if workspace:
                out[str(chat_id)] = workspace
    return out


def _provider_interleaved(
    bucket: list[Candidate], rng: random.Random
) -> list[Candidate]:
    """Order one bucket so neighbouring candidates come from different providers.

    A tercile is not a provider population: the same chat can be a five-minute
    Claude question or a day-long opencode session, so slicing by length leaves
    provider skew inside each slice. Walking the providers round-robin (each
    provider's own order drawn from the seeded shuffle) means no prefix of the
    bucket is one provider's territory.
    """
    by_provider: dict[str, list[Candidate]] = {}
    for cand in bucket:
        by_provider.setdefault(cand.provider, []).append(cand)
    for chat_list in by_provider.values():
        rng.shuffle(chat_list)
    remaining = [by_provider[name] for name in sorted(by_provider)]
    ordered: list[Candidate] = []
    while remaining:
        for chat_list in remaining:
            if chat_list:
                ordered.append(chat_list.pop())
        remaining = [chat_list for chat_list in remaining if chat_list]
    return ordered


def _varied_sample(
    pool: list[Candidate], last: int, seed: int
) -> list[Candidate]:
    """Pick ``last`` chats spread over length and provider.

    The newest N archives are the ones the user has actually been living
    with, and a straight "newest 50" is a length-ordered sample in
    practice — recent sessions are short. So the pool is sorted by transcript
    length, split into three length terciles, each tercile is ordered so
    neighbouring picks come from different providers, and the three are then
    drawn round-robin. The result still favours recent chats but reaches the
    long ones, and with ``seed`` fixed the same command twice picks the same
    chats.
    """
    # Ascending by length, and stable, so equal-length chats stay in
    # newest-first order: recency is the tie-breaker that was there before.
    by_length = sorted(pool, key=lambda c: c.chars)
    size = max(1, len(by_length) // 3)
    rng = random.Random(seed)
    buckets = [
        _provider_interleaved(chunk, rng)
        for chunk in (
            by_length[:size],
            by_length[size : size * 2],
            by_length[size * 2 :],
        )
    ]
    picked: list[Candidate] = []
    index = 0
    while len(picked) < last and any(buckets):
        bucket = buckets[index % len(buckets)]
        if bucket:
            picked.append(bucket.pop())
        index += 1
    return picked


def select_archives(
    config: Any,
    *,
    workspace: str = "",
    last: int = 50,
    sample: str = "varied",
    seed: int = 0,
) -> list[Candidate]:
    """Archived chats worth running the comparison over, newest first.

    ``Chats/<chat id>/<provider>/*.md``, the same tree backfill walks, so
    every archive the server can extract from is a candidate here. The chat's
    provider is the directory name, which is the only place it is recorded.

    ``sample="varied"`` spreads the picks over transcript length and
    provider; ``"recent"`` takes the newest ``last``. Either way ``last``
    bounds the cost: two model calls per chat, and the agent one is a
    multi-turn run.
    """
    from ciao import provider_registry
    from ciao.insights import locate_insights_section

    limit = max(0, int(last))
    if limit == 0:
        return []
    base = config.logs_root / "Chats"
    by_chat = _chat_workspaces(config.state_path.parent)
    found: list[Candidate] = []
    for md in base.glob("*/*/*.md"):
        provider = md.parent.name
        if not provider_registry.is_provider(provider):
            continue
        chat_id = md.parent.parent.name
        chat_workspace = by_chat.get(chat_id, "")
        if workspace and chat_workspace != workspace:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.info("insights-compare: could not read %s", md)
            continue
        body = strip_insights(text) if locate_insights_section(text) else text
        if len(body.strip()) < _MIN_ARCHIVE_CHARS:
            continue
        found.append(
            Candidate(
                archive_path=md,
                chat_id=chat_id,
                provider=provider,
                workspace=chat_workspace,
                chars=len(body),
            )
        )
    found.sort(key=lambda c: c.archive_path.stat().st_mtime, reverse=True)
    pool = found[: limit * _VARIED_POOL_MULTIPLIER] if sample == "varied" else found
    if sample == "varied":
        return _varied_sample(pool, limit, seed)
    return pool[:limit]


def _cache_name(cand: Candidate) -> str:
    return f"{cand.chat_id}__{cand.archive_path.stem}.json"


def _route(output: str, vault_root: Path, guide: Path | None) -> tuple[list[dict], int, int]:
    """Route one mode's output through the shared, write-free routing."""
    from ciao.memory_proposals import route_insights

    routed = route_insights(output, vault_root, guide_path=guide)
    kept = [
        {"target": p.target, "payload": p.payload, "text": p.text} for p in routed.kept
    ]
    return kept, len(routed.suppressed), routed.dropped


def _routed(output: str, vault_root: Path, guide: Path | None) -> dict:
    """The routing half of a mode result, as a dict of fields to set.

    A routing failure is that mode's error, not the run's: a model that
    answered with prose still tells the operator something.
    """
    try:
        kept, suppressed, dropped = _route(output, vault_root, guide)
    except Exception as exc:  # noqa: BLE001 — one bad body must not end the run
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
    return {"kept": kept, "suppressed": suppressed, "dropped": dropped}


async def _timed(call: Callable[[], Awaitable[Any]]) -> tuple[Any, str, float]:
    """Await one mode's call; a failure becomes a recorded error, not a raise.

    The comparison's whole value is a report over 50 chats. One chat whose
    provider call raised must not cost the other 49.
    """
    started = time.monotonic()
    try:
        return await call(), "", round(time.monotonic() - started, 2)
    except Exception as exc:  # noqa: BLE001 — a failed mode is a recorded result
        return None, f"{type(exc).__name__}: {exc}"[:300], round(
            time.monotonic() - started, 2
        )


@dataclass
class _Prep:
    """What both modes need from the vault, the model config and the archive.

    Resolved once per chat, before either mode runs, and outside them: a chat
    whose model cannot be resolved has neither mode to run, so failing here is
    a per-chat error rather than a per-mode one.
    """

    vault_root: Path
    guide: Path | None
    model: str
    eff_provider: str
    body: str
    context_block: str


def _prepare(config: Any, cand: Candidate) -> _Prep:
    """Read the archive and resolve the model both modes will use.

    Every step here can fail on a single chat — the archive may have been
    reclaimed since selection, the workspace may no longer resolve, the model
    lookup may raise. The caller turns any of those into a recorded error for
    this chat alone.
    """
    from ciao.insights import (
        _known_context_block,
        _resolve_insights_call,
        resolve_insights_model,
    )

    vault_root = (
        config.workspace_vault_root(cand.workspace)
        if cand.workspace
        else config.vault_root
    )
    guide: Path | None = None
    if cand.workspace:
        from ciao.workspace_guide import guide_path

        guide = guide_path(config.agent_root(cand.workspace))

    provider_models = getattr(config, "provider_insights_models", {}) or {}
    model = provider_models.get(cand.provider, "") or resolve_insights_model(
        config, cand.workspace or None, cand.provider
    )
    model, eff_provider, _note = _resolve_insights_call(
        config, model, provider=cand.provider
    )
    body = strip_insights(cand.archive_path.read_text(encoding="utf-8"))
    return _Prep(
        vault_root=vault_root,
        guide=guide,
        model=model,
        eff_provider=eff_provider,
        body=body,
        context_block=_known_context_block(guide, vault_root, transcript=body),
    )


def _error_mode(mode: str, error: str) -> ModeResult:
    return ModeResult(
        mode=mode, status="error", seconds=0.0, output="",
        kept=[], suppressed=0, dropped=0, error=error[:300],
    )


async def _oneshot_result(config: Any, prep: _Prep) -> ModeResult:
    """Exactly today's extraction path: same body, context block and model.

    The only thing that differs from the agent run is the tool surface, so
    anything that goes wrong here is a fact about one-shot mode, not about
    this chat.
    """
    from ciao.insights import _call_text_model

    try:
        text, error, seconds = await _timed(
            lambda: _call_text_model(
                prep.body, prep.model, provider=prep.eff_provider,
                cwd=config.workspace_root, context_block=prep.context_block,
            )
        )
        result = ModeResult(
            mode="oneshot", status="ok", seconds=seconds, output="",
            kept=[], suppressed=0, dropped=0, error=error,
        )
        if text is None:
            result.status = "error"
            return result
        result.output = text
        _apply_routed(result, _routed(text, prep.vault_root, prep.guide))
        return result
    except Exception as exc:  # noqa: BLE001 — a failed mode is a recorded result
        return _error_mode("oneshot", f"{type(exc).__name__}: {exc}")


async def _agent_result(cand: Candidate, prep: _Prep) -> ModeResult:
    """The read-only agent run, or a recorded reason there was not one."""
    if prep.eff_provider not in _AGENT_PROVIDERS:
        return ModeResult(
            mode="agent", status=f"skipped: {prep.eff_provider}", seconds=0.0,
            output="", kept=[], suppressed=0, dropped=0,
        )
    try:
        from ciao.insights_agent import run_agent_extraction

        run, error, seconds = await _timed(
            lambda: run_agent_extraction(
                cand.archive_path,
                vault_root=prep.vault_root,
                guide_path=prep.guide,
                model=prep.model,
                provider=prep.eff_provider,
            )
        )
        result = ModeResult(
            mode="agent", status="ok", seconds=seconds, output="",
            kept=[], suppressed=0, dropped=0, error=error,
        )
        if run is None:
            result.status = "error"
            return result
        result.output = run.text
        result.turns = run.turns
        result.tool_calls = run.tool_calls
        result.cost_usd = run.cost_usd
        result.denied = run.denied
        _apply_routed(result, _routed(run.text, prep.vault_root, prep.guide))
        return result
    except Exception as exc:  # noqa: BLE001 — a failed mode is a recorded result
        return _error_mode("agent", f"{type(exc).__name__}: {exc}")


def _unprepared_record(cand: Candidate, exc: Exception) -> dict:
    """The record for a chat that could not be prepared for either mode.

    Both modes are reported as errors rather than one being invented: the
    report counts ok/error per mode, and a chat missing from it would read as
    a mode that was never tried. Cached with the rest, so a resume does not
    re-attempt a candidate that failed before it ever called a model.
    """
    error = f"{type(exc).__name__}: {exc}"[:300]
    return {
        "chat_id": cand.chat_id,
        "provider": cand.provider,
        "workspace": cand.workspace,
        "chars": cand.chars,
        "model": "",
        "effective_provider": "",
        "oneshot": _mode_to_dict(_error_mode("oneshot", error)),
        "agent": _mode_to_dict(_error_mode("agent", error)),
    }


async def compare_one(config: Any, cand: Candidate, *, cache_dir: Path) -> dict:
    """Run both modes over one archived chat and return its result record.

    Resumable by construction: a chat whose record is already in
    ``cache_dir`` is returned as-is without a model call, so a run
    interrupted at chat 40 of 50 resumes at 41 rather than paying twice.

    This chat's problems end here. Preparation, one-shot mode and agent mode
    each record their own failure, because ``asyncio.gather`` over 50 of
    these turns one unhandled raise into a run with no report at all.
    """
    cache_path = cache_dir / _cache_name(cand)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        cached = None
    if isinstance(cached, dict):
        return cached

    try:
        prep = _prepare(config, cand)
    except Exception as exc:  # noqa: BLE001 — one bad candidate is a result, not a raise
        record = _unprepared_record(cand, exc)
    else:
        oneshot = await _oneshot_result(config, prep)
        agent = await _agent_result(cand, prep)
        record = {
            "chat_id": cand.chat_id,
            "provider": cand.provider,
            "workspace": cand.workspace,
            "chars": cand.chars,
            "model": prep.model,
            "effective_provider": prep.eff_provider,
            "oneshot": _mode_to_dict(oneshot),
            "agent": _mode_to_dict(agent),
        }
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        logger.info("insights-compare: could not cache %s", cache_path)
    return record


def _apply_routed(result: ModeResult, routed: dict) -> None:
    """Set a mode's routing counts, or its error when routing itself failed."""
    if "status" in routed:
        result.status = routed["status"]
        result.error = routed["error"]
        return
    result.kept = routed["kept"]
    result.suppressed = routed["suppressed"]
    result.dropped = routed["dropped"]


def _mode_to_dict(result: ModeResult) -> dict:
    return {
        "mode": result.mode,
        "status": result.status,
        "seconds": result.seconds,
        "output": result.output,
        "kept": result.kept,
        "suppressed": result.suppressed,
        "dropped": result.dropped,
        "turns": result.turns,
        "tool_calls": result.tool_calls,
        "cost_usd": result.cost_usd,
        "denied": result.denied,
        "error": result.error,
    }


async def run_compare(
    config: Any,
    candidates: Sequence[Candidate],
    *,
    cache_dir: Path,
    concurrency: int = 3,
    progress: Callable[[str], None] = print,
) -> list[dict]:
    """Run the comparison over ``candidates``, a few chats at a time.

    Concurrency is bounded rather than equal to the list length: the agent
    mode spawns a provider run per chat, and fifty of those at once is a
    rate-limit story. The progress line reports the running agent cost so a
    long run's price is visible while it is happening, not only in the
    report at the end.

    A candidate that still raises — ``compare_one`` records the failures it
    knows about, and this is the backstop for one it does not — becomes an
    error record in place. ``asyncio.gather`` otherwise cancels its siblings
    on the first raise, which would cost the whole report for one chat.
    """
    total = len(candidates)
    sem = asyncio.Semaphore(max(1, int(concurrency)))
    state = {"done": 0, "spent": 0.0}

    async def one(cand: Candidate) -> dict:
        async with sem:
            try:
                record = await compare_one(config, cand, cache_dir=cache_dir)
            except Exception as exc:  # noqa: BLE001 — a bad candidate is a result
                logger.info(
                    "insights-compare: %s could not be compared: %s",
                    cand.chat_id, exc,
                )
                record = _unprepared_record(cand, exc)
        state["done"] += 1
        agent = record.get("agent")
        agent = agent if isinstance(agent, dict) else {}
        cost = agent.get("cost_usd")
        if isinstance(cost, (int, float)):
            state["spent"] += float(cost)
        progress(
            f"[{state['done']}/{total}] {cand.chat_id} "
            f"oneshot={_status(record.get('oneshot'))} "
            f"{_seconds(record.get('oneshot'))}s "
            f"agent={_status(agent)} {_seconds(agent)}s "
            f"cost_so_far=${state['spent']:.4f}"
        )
        return record

    return list(await asyncio.gather(*(one(cand) for cand in candidates)))


def _status(mode: Any) -> str:
    if not isinstance(mode, dict):
        return "error"
    return str(mode.get("status") or "error")


def _seconds(mode: Any) -> str:
    if not isinstance(mode, dict):
        return "0"
    return str(mode.get("seconds") or 0)


def _median(values: Sequence[float]) -> str:
    if not values:
        return "-"
    return f"{statistics.median(values):.1f}"


def _totals_row(label: str, modes: Sequence[dict]) -> str:
    chats = len(modes)
    ok = sum(1 for m in modes if m.get("status") == "ok")
    errors = sum(1 for m in modes if m.get("status") == "error")
    skipped = chats - ok - errors
    kept = [m for m in modes if m.get("status") == "ok"]
    seconds = [
        float(m.get("seconds") or 0) for m in kept
    ]
    by_target = {
        target: sum(
            1 for m in kept for p in (m.get("kept") or []) if p.get("target") == target
        )
        for target in _REPORT_TARGETS
    }
    suppressed = sum(int(m.get("suppressed") or 0) for m in modes)
    dropped = sum(int(m.get("dropped") or 0) for m in modes)
    cells = [
        label,
        str(chats),
        str(ok),
        str(errors),
        str(skipped),
        _median(seconds),
        str(sum(len(m.get("kept") or []) for m in kept)),
        *(str(by_target[t]) for t in _REPORT_TARGETS),
        str(suppressed),
        str(dropped),
    ]
    if label == "agent":
        cost = sum(
            float(m.get("cost_usd") or 0) for m in modes
        )
        tool_calls = [
            float(m.get("tool_calls") or 0) for m in modes if m.get("status") == "ok"
        ]
        cells.append(f"${cost:.4f}")
        cells.append(_median(tool_calls))
    else:
        # Same table shape for both rows: a one-shot has no cost and no tool
        # calls, and a ragged row is one more thing to read past.
        cells.extend(["-", "-"])
    return "| " + " | ".join(cells) + " |"


_TOTALS_HEADER = (
    "| mode | chats | ok | errors | skipped | median s | kept "
    + " | ".join(_REPORT_TARGETS)
    + " | suppressed | dropped"
    + " | cost | median tool calls |"
)
_TOTALS_RULE = "|---" * 17 + "|"


def render_report(
    results: Sequence[dict], *, workspace: str = "", started: str = ""
) -> str:
    """Render the comparison as a Markdown note for the vault.

    Two tables and one section per chat, and no verdict. The run's premise is
    that the operator reads both extractions and decides which is better; a
    summary that scored them for them would be the thing under test.
    """
    today = date.today().isoformat()
    lines: list[str] = [
        "---",
        "type: reference",
        f"updated: {today}",
        "tags: [insights-compare]",
        "---",
        "",
        f"# Insights compare ({workspace or 'all workspaces'})",
        "",
        f"Run started: {started or today}. One-shot is today's extraction path; "
        "agent is the same extraction with Read/Grep/Glob over the vault. "
        "Both are dry runs: nothing was applied.",
        "",
        "## Totals",
        "",
    ]
    by_provider: dict[str, list[dict]] = {}
    for record in results:
        by_provider.setdefault(str(record.get("provider") or "?"), []).append(record)
    for provider in sorted(by_provider):
        records = by_provider[provider]
        lines.append(f"### {provider}")
        lines.append("")
        lines.append(_TOTALS_HEADER)
        lines.append(_TOTALS_RULE)
        lines.append(_totals_row("oneshot", [r.get("oneshot") or {} for r in records]))
        lines.append(_totals_row("agent", [r.get("agent") or {} for r in records]))
        lines.append("")

    lines.append("## Chats")
    lines.append("")
    for record in results:
        chars = record.get("chars")
        lines.append(
            f"### {record.get('chat_id')} ({record.get('provider')}, {chars} chars)"
        )
        lines.append("")
        for key, label in (("oneshot", "One-shot"), ("agent", "Agent")):
            mode = record.get(key) or {}
            lines.append(f"**{label}** — {mode.get('status')}, {mode.get('seconds')}s")
            lines.append("")
            for proposal in mode.get("kept") or []:
                target = proposal.get("target") or ""
                payload = proposal.get("payload") or ""
                suffix = f": {payload}" if payload else ""
                lines.append(f"- [{target}{suffix}] {proposal.get('text') or ''}")
            if not (mode.get("kept") or []):
                lines.append("- (no kept proposals)")
            if mode.get("error"):
                lines.append(f"- error: {mode['error']}")
            if key == "agent" and mode.get("status") == "ok":
                lines.append(
                    f"- {mode.get('tool_calls') or 0} tool calls, "
                    f"{mode.get('denied') or 0} denied, "
                    f"${float(mode.get('cost_usd') or 0):.4f}"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_report_path(vault_root: Path, today: str) -> Path:
    """Where a run's report lands when ``--out`` was not given."""
    return vault_root / "Workspace" / f"Insights-Compare-{today}.md"
