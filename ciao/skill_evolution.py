"""Skill evolution: mine trajectories and propose skill edits.

Runs weekly (Sunday night) via ``.runtime/schedules.json``. The pass:

1. Loads trajectories from the last N days via
   :mod:`ciao.trajectory_builder`.
2. Groups by skill and flags any skill that appeared in a session whose
   outcome wasn't clean (errors > 0 or user_corrections > 0 or
   outcome != ``success``).
3. For each flagged skill, runs a small DAG: propose via
   ``kimi-k2.7-code:cloud`` through Pi → semantic check (LLM-as-judge,
   same model) → test gate (pytest on ``tests/test_<skill>.py``) →
   write a draft proposal Markdown. The DAG helper is in
   :mod:`ciao.dag`; per-node timing lands in ``.runtime/job_runs.jsonl``
   with ``provider='dag'`` so the Automation page can drill in.
4. Writes one Markdown proposal per skill to
   ``memory-vault/personal/Workspace/Skill-Proposals/YYYY-MM-DD-<skill>.md``.

Guardrails:

* **Size**: skills already over 15 KB trigger a trim-mode proposal
  (the model is told to propose deletions, not additions).
* **Test gate** (optional, ``enable_test_gate``): if ``tests/test_<skill>.py``
  exists, it must pass before a proposal is written.
* **Semantic check**: encoded in the system prompt -- the model must not
  drift the skill's core purpose. Phase 1 leaves this to human review.
* **No auto-apply**: proposals land as draft Markdown for review.

Why a DAG: the per-skill pipeline is the canonical example of the
"deterministic multi-model schedule" pattern in
``Resources/Archon.md`` (decision: do-not-adopt; pattern: steal). The
DAG is local Python, no new runtime, and gives per-node model + timing
visibility in the existing job_runs log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ciao.providers.oneshot import run_oneshot
from ciao.providers.ollama import OllamaSettings, routine_env_for_model
from ciao.providers.routing import routing_routine_env_for_model
from ciao.trajectory_builder import (
    DEFAULT_RETENTION_MONTHS,
    list_trajectories,
    load_trajectory,
    prune_old,
)
from ciao.dag import Edge, Node, run as run_dag

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SKILLS_ROOT = _REPO_ROOT / "skills"
# The project also ships skills under ``.claude/skills/`` (synced from
# ``skills/`` by ``ciao/upgrade.py``). Some skills live only in one of the
# two roots, so the resolver searches both. Order matters: ``skills/`` is
# the canonical edit target for the upgrade flow, so we prefer it.
_DEFAULT_SKILLS_ROOTS: tuple[Path, ...] = (
    _REPO_ROOT / "skills",
    _REPO_ROOT / ".claude" / "skills",
)
_DEFAULT_PROPOSALS_DIR = (
    _REPO_ROOT / "memory-vault" / "personal" / "Workspace" / "Skill-Proposals"
)
_DEFAULT_TESTS_ROOT = _REPO_ROOT / "tests"

MAX_SKILL_BYTES = 15 * 1024
MAX_TRAJECTORIES_PER_PROMPT = 10


PROPOSAL_SYSTEM_PROMPT = """\
You are a skill editor. You are given a skill file (SKILL.md) and a set
of execution trajectories where this skill was LOADED in a session that
did not end cleanly (errors, user pushback, or non-success outcome).

A skill being "loaded" in a failing session is a weak signal. The
session may have loaded this skill alongside other skills, and the
failure may have been caused by a different skill, a tool call, or the
user's request — not by anything in SKILL.md.

Your job: decide whether the failure was caused by this skill, and if
so, propose a concrete edit to the skill that would have prevented or
improved the failure.

Decision procedure (follow this order):
1. Read the ``errors[].snippet`` fields. If they are about a tool call,
   parameter, or stack trace that has nothing to do with this skill's
   primary workflow, this skill is probably not the cause.
2. Read the ``tools_used`` list. If the skill was never actually used
   (no ``Skill`` tool invocation tied to it, no other signal that its
   guidance was followed), the session's failure is likely unrelated.
3. Read the ``task_summary`` and ``user_corrections``. If those point to
   a different topic than this skill's domain, this skill is probably
   not the cause.
4. Only if the trajectory gives a clear, specific link between this
   skill and the failure should you propose a fix.

If the trajectory does not establish a causal link, reply with the
single line: ``No clear improvement found.`` Do NOT invent a fix for an
unrelated failure. Inventing a fix is worse than declining.

Rules when you DO have a causal link:
- Propose specific text changes, not vague advice.
- Keep the skill under 15KB total.
- Do not change the skill's core purpose. The same triggers must still fire.
- Output one of:
  * a unified Markdown diff (preferred), OR
  * a replacement block tagged with the heading it replaces.
- End with a single line: ``confidence: 0.0-1.0`` reflecting how sure
  you are the edit would help.

If you cannot establish a causal link, end with the single line:
``No clear improvement found.``
"""


# Used when the skill is already over MAX_SKILL_BYTES. The default
# prompt's "keep the skill under 15KB" rule is unsatisfiable for
# skills that start over the cap, so it would force the model to refuse
# to add anything. The trim prompt reframes the goal: bring the skill
# under the cap by removing redundancy, while preserving the primary
# workflow and trigger conditions. This is the case where a proposal
# is most valuable (bloated skills are usually bloated in ways that
# also cause the failures the trajectories show).
TRIM_PROPOSAL_SYSTEM_PROMPT = """\
You are a skill editor. You are given a skill file (SKILL.md) and a set
of execution trajectories where this skill was LOADED in a session that
did not end cleanly (errors, user pushback, or non-success outcome).

A skill being "loaded" in a failing session is a weak signal. The
session may have loaded this skill alongside other skills, and the
failure may have been caused by a different skill, a tool call, or the
user's request — not by anything in SKILL.md.

The skill is currently OVER the 15KB size cap ({current_bytes} bytes).
Your job: decide whether the trajectory failure is related to this
skill, and if so, propose concrete TRIM edits that bring the skill
under the cap.

Decision procedure (follow this order):
1. Read the ``errors[].snippet`` fields. If they are about a tool call,
   parameter, or stack trace that has nothing to do with this skill's
   primary workflow, this skill is probably not the cause.
2. Read the ``tools_used`` list. If the skill was never actually used,
   the session's failure is likely unrelated.
3. Read the ``task_summary`` and ``user_corrections``. If those point to
   a different topic than this skill's domain, this skill is probably
   not the cause.
4. Only if the trajectory gives a clear link between this skill and
   the failure should you propose a trim.

If the trajectory does not establish a causal link, reply with the
single line: ``No clear improvement found.``

Rules when you DO have a causal link:
- Propose specific text deletions and consolidations, not vague advice.
- The skill MUST end up under 15360 bytes after your edits.
- Do not change the skill's core purpose. The same triggers must still
  fire, and the primary workflow must remain intact.
- Prefer removing redundant examples, verbose explanations, and
  boilerplate that does not change behavior. Collapse near-duplicate
  sections. Consolidate multi-step instructions that already follow
  from a single example.
- Do not strip the trigger conditions, the safety/guardrail block, or
  the test-gate references.
- Output one of:
  * a unified Markdown diff (preferred), OR
  * a replacement block tagged with the heading it replaces.
- End with two lines:
  ``projected_bytes: <integer>`` — your estimate of the skill size in
  bytes after the proposed edits are applied.
  ``confidence: 0.0-1.0`` — how sure you are the trim preserves the
  skill's primary purpose and would help the failing trajectories.
- If no safe trim is obvious, end with the single line:
  ``No clear improvement found.``
"""


SEMANTIC_CHECK_SYSTEM_PROMPT = """\
You are a strict skill-drift judge. You are given the original skill
file and a proposed edit. Your only job is to decide whether the edit
preserves the skill's core purpose:

- The same trigger conditions still fire the skill.
- The same primary task is the skill's main job.
- The edit does not narrow, broaden, or repurpose the skill.

Style edits, clarifications, bug fixes, and small guidance additions
all PRESERVE the core purpose. Anything that changes the skill's
domain, swaps its trigger keywords, or removes/replaces its primary
workflow DRIFTS.

Output EXACTLY two lines:
VERDICT: PRESERVED
REASON: <one sentence>

or:
VERDICT: DRIFTED
REASON: <one sentence>

No other output.
"""


# ── Trajectory mining ────────────────────────────────────────────────────


def _is_underperforming(trajectory: dict[str, Any]) -> bool:
    outcome = trajectory.get("outcome")
    if outcome and outcome != "success":
        return True
    if (trajectory.get("user_corrections") or 0) > 0:
        return True
    if trajectory.get("errors"):
        return True
    return False


def find_underperforming_skills(
    trajectories: list[dict[str, Any]],
    *,
    min_sessions: int = 1,
) -> dict[str, list[dict[str, Any]]]:
    """Group underperforming trajectories by skill.

    Returns a dict ``{skill_name: [trajectory, ...]}``. Skills with fewer
    than ``min_sessions`` matching trajectories are filtered out.
    """
    by_skill: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for traj in trajectories:
        if not _is_underperforming(traj):
            continue
        for skill in traj.get("skills_loaded") or []:
            if not isinstance(skill, str) or not skill.strip():
                continue
            by_skill[skill].append(traj)
    return {
        name: recs
        for name, recs in by_skill.items()
        if len(recs) >= min_sessions
    }


# ── Skill file resolution + guardrails ──────────────────────────────────


def find_skill_file(skill_name: str, skills_root: Path) -> Path | None:
    """Locate the SKILL.md (or <name>.md) for a skill name.

    Single-root form: searches ``skills_root`` only. Kept for callers
    and tests that explicitly pin a root.
    """
    return find_skill_file_in_roots(skill_name, [skills_root])


def find_skill_file_in_roots(
    skill_name: str,
    skills_roots: Iterable[Path],
) -> Path | None:
    """Locate the SKILL.md (or <name>.md) for a skill name across roots.

    Tries each root in order, returning the first match. The caller
    should pass roots in priority order: ``skills/`` first
    (canonical edit target), then the synced read-only mirror under
    ``.claude/skills/``.
    """
    safe = skill_name.strip()
    if not safe:
        return None
    for root in skills_roots:
        for candidate in (root / safe / "SKILL.md", root / f"{safe}.md"):
            if candidate.is_file():
                return candidate
    return None


def passes_size_check(text: str, *, max_bytes: int = MAX_SKILL_BYTES) -> bool:
    return len(text.encode("utf-8")) <= max_bytes


def find_skill_tests(
    skill_name: str,
    *,
    tests_root: Path | None = None,
) -> list[Path]:
    """Locate pytest files that look like they cover this skill."""
    root = tests_root or _DEFAULT_TESTS_ROOT
    if not root.exists():
        return []
    underscored = skill_name.replace("-", "_")
    patterns = [f"test_{skill_name}.py", f"test_{underscored}.py"]
    found: list[Path] = []
    for pat in patterns:
        found.extend(root.glob(pat))
    # de-duplicate while preserving order
    seen: set[Path] = set()
    out: list[Path] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def run_skill_tests(
    test_files: Iterable[Path],
    *,
    timeout_s: int = 300,
) -> bool:
    """Run pytest on the given files. ``True`` = all passed (or no tests)."""
    paths = [str(f) for f in test_files]
    if not paths:
        return True
    try:
        proc = subprocess.run(
            ["python3", "-m", "pytest", "-q", *paths],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Skill test run failed: %s", exc)
        return False
    if proc.returncode != 0:
        logger.info(
            "Skill tests failed (exit=%d): %s",
            proc.returncode,
            proc.stdout[-400:],
        )
        return False
    return True


# ── Model call ───────────────────────────────────────────────────────────


def _trim_trajectory(traj: dict[str, Any]) -> dict[str, Any]:
    """Drop heavy fields before serialising into the proposal prompt."""
    return {
        "session_id": (traj.get("session_id") or "")[:8],
        "timestamp": traj.get("timestamp"),
        "outcome": traj.get("outcome"),
        "turns": traj.get("turns"),
        "user_corrections": traj.get("user_corrections"),
        "errors": traj.get("errors") or [],
        "decisions": traj.get("decisions") or [],
        "tools_used": traj.get("tools_used") or [],
        "task_summary": traj.get("task_summary"),
    }


async def propose_skill_edit(
    skill_path: Path,
    trajectories: list[dict[str, Any]],
    *,
    env: dict[str, str] | None = None,
    model: str,
    timeout_s: float = 180.0,
    force_trim: bool = False,
) -> str | None:
    """Ask a cheap model for an edit proposal. Returns the proposal text or None.

    Returns ``None`` when Pi isn't installed, the model returns nothing,
    or the model explicitly says ``No clear improvement found.``.

    When ``force_trim`` is true (skill is over the size cap), uses the
    trim prompt so the model is told to propose deletions that bring
    the skill under the cap, instead of an unsatisfiable "keep under
    15KB by adding helpful guidance" instruction.
    """
    skill_text = skill_path.read_text(encoding="utf-8")
    trimmed = [_trim_trajectory(t) for t in trajectories[:MAX_TRAJECTORIES_PER_PROMPT]]
    trajectories_text = json.dumps(trimmed, indent=2, ensure_ascii=False)
    if force_trim:
        system_prompt = TRIM_PROPOSAL_SYSTEM_PROMPT.format(
            current_bytes=len(skill_text.encode("utf-8"))
        )
    else:
        system_prompt = PROPOSAL_SYSTEM_PROMPT
    prompt = (
        f"Skill file ({skill_path.name}):\n"
        "```markdown\n"
        f"{skill_text}\n"
        "```\n\n"
        "Execution trajectories where this skill underperformed:\n"
        "```json\n"
        f"{trajectories_text}\n"
        "```\n\n"
        "Propose improvements:"
    )
    try:
        result = await run_oneshot(
            prompt,
            system_prompt=system_prompt,
            model=model,
            env=env,
            timeout_s=timeout_s,
        )
    except (TimeoutError, OSError, RuntimeError) as exc:
        logger.warning("Proposal call failed for %s: %s", skill_path, exc)
        return None
    cleaned = (result or "").strip()
    if not cleaned:
        return None
    if cleaned.lower().startswith("no clear improvement"):
        return None
    return cleaned


# ── Semantic drift gate ──────────────────────────────────────────────────


async def passes_semantic_check(
    skill_path: Path,
    proposal_text: str,
    *,
    env: dict[str, str] | None = None,
    model: str,
    timeout_s: float = 60.0,
) -> tuple[bool, str]:
    """Second-pass judge: does the proposal preserve the skill's core purpose?

    Cheaper than the proposal call (smaller prompt, short answer) and
    runs against the same upstream as the proposal. Returns
    ``(passed, reason)``; on a model failure we fail-open with
    passed=True since the proposal still has to clear human review.
    """
    skill_excerpt = skill_path.read_text(encoding="utf-8")[:1200]
    prompt = (
        f"Original skill ({skill_path.name}, first 1200 chars):\n"
        "```markdown\n"
        f"{skill_excerpt}\n"
        "```\n\n"
        "Proposed edit:\n"
        "```\n"
        f"{proposal_text[:2000]}\n"
        "```\n\n"
        "Judge:"
    )
    try:
        result = await run_oneshot(
            prompt,
            system_prompt=SEMANTIC_CHECK_SYSTEM_PROMPT,
            model=model,
            env=env,
            timeout_s=timeout_s,
        )
    except (TimeoutError, OSError, RuntimeError) as exc:
        logger.warning("Semantic check call failed for %s: %s", skill_path, exc)
        # Fail-open: don't lose a proposal because the judge timed out.
        return True, f"semantic check unavailable: {exc}"
    text = (result or "").strip()
    if not text:
        return True, "semantic check returned no output"
    verdict_line = next(
        (line for line in text.splitlines() if line.upper().startswith("VERDICT:")),
        "",
    )
    reason_line = next(
        (line for line in text.splitlines() if line.upper().startswith("REASON:")),
        "",
    )
    reason = reason_line.split(":", 1)[1].strip() if ":" in reason_line else ""
    if "DRIFTED" in verdict_line.upper():
        return False, reason or "model flagged drift"
    if "PRESERVED" in verdict_line.upper():
        return True, reason or "preserved"
    # Unparseable answer: fail-open with the raw verdict line so the
    # reviewer can see what happened in the proposal frontmatter.
    return True, f"unparseable verdict: {verdict_line[:80]}"


# ── Proposal writing ─────────────────────────────────────────────────────


def write_proposal(
    *,
    skill_name: str,
    skill_path: Path,
    trajectories: list[dict[str, Any]],
    proposal_text: str,
    output_dir: Path,
    now: datetime | None = None,
    semantic_verdict: str = "",
    semantic_reason: str = "",
) -> Path:
    """Render the proposal Markdown and write it to ``output_dir``."""
    ts = now or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{ts.strftime('%Y-%m-%d')}-{skill_name}.md"

    rows = "\n".join(
        f"- {(t.get('session_id') or '')[:8]} "
        f"outcome={t.get('outcome', '?')} "
        f"corrections={t.get('user_corrections', 0)} "
        f"errors={len(t.get('errors') or [])} "
        f"turns={t.get('turns', 0)}"
        for t in trajectories
    )

    semantic_block = ""
    if semantic_verdict:
        semantic_block = (
            f"semantic_check: {semantic_verdict}\n"
            f"semantic_reason: {semantic_reason}\n"
        )

    body = (
        f"---\n"
        f"type: skill-proposal\n"
        f"skill: {skill_name}\n"
        f"status: draft\n"
        f"generated: {ts.isoformat().replace('+00:00', 'Z')}\n"
        f"trajectories: {len(trajectories)}\n"
        f"{semantic_block}"
        f"---\n\n"
        f"# Skill proposal: {skill_name}\n\n"
        f"- **Skill path:** `{skill_path}`\n"
        f"- **Generated:** {ts.isoformat().replace('+00:00', 'Z')}\n"
        f"- **Trajectories analyzed:** {len(trajectories)}\n"
    )
    if semantic_verdict:
        body += f"- **Semantic check:** {semantic_verdict} — {semantic_reason}\n"
    body += (
        f"\n## Source trajectories\n\n"
        f"{rows}\n\n"
        f"## Proposal\n\n"
        f"{proposal_text}\n"
    )
    path.write_text(body, encoding="utf-8")
    logger.info("Wrote skill proposal %s", path)
    return path


# ── Main pass ────────────────────────────────────────────────────────────


async def _process_skill_dag(
    skill_name: str,
    skill_path: Path,
    skill_trajectories: list[dict[str, Any]],
    *,
    output_dir: Path,
    env: dict[str, str] | None,
    model: str,
    now: datetime,
    enable_test_gate: bool,
    enable_semantic_check: bool,
    tests_root: Path | None,
) -> Path | None:
    """Run the per-skill DAG and return the written proposal path, or
    ``None`` if no proposal was written (e.g. semantic drift, test
    failure, no improvement found and not over-cap).

    The async work (proposal generation, semantic check) happens before
    we build the DAG, because the DAG executor in :mod:`ciao.dag` is
    sync. We then wire the cached results through ``gate`` nodes so
    per-node timing still lands in ``job_runs.jsonl`` (the gates each
    get a row).
    """
    skill_text = skill_path.read_text(encoding="utf-8")
    over_cap = not passes_size_check(skill_text)
    if over_cap:
        logger.info(
            "Skill %s is over %d bytes; running trim-mode proposal",
            skill_name,
            MAX_SKILL_BYTES,
        )

    proposal = await propose_skill_edit(
        skill_path,
        skill_trajectories,
        env=env,
        model=model,
        force_trim=over_cap,
    )

    semantic_verdict = ""
    semantic_reason = ""
    semantic_ok = True  # default: pass when semantic check is disabled
    if proposal and enable_semantic_check:
        semantic_ok, semantic_reason = await passes_semantic_check(
            skill_path,
            proposal,
            env=env,
            model=model,
        )
        semantic_verdict = "PRESERVED" if semantic_ok else "DRIFTED"

    # The DAG gates on cached results. Each gate gets a job_runs row.
    def proposal_present(ctx: dict[str, Any]) -> tuple[bool, str]:
        return (proposal is not None), "proposal" if proposal else "no-proposal"

    def semantic_passed(ctx: dict[str, Any]) -> tuple[bool, str]:
        return semantic_ok, semantic_verdict or "skipped"

    def tests_passed(ctx: dict[str, Any]) -> tuple[bool, str]:
        if not enable_test_gate:
            return True, "test-gate-disabled"
        tests = find_skill_tests(skill_name, tests_root=tests_root)
        if not tests:
            return True, "no-tests"
        return run_skill_tests(tests), "tests-ran"

    # The write nodes run inside the DAG (gate executor runs the
    # callable and stores the path in ctx), then we read the path
    # back from ctx after the run.
    written_path: dict[str, str | None] = {"value": None}

    def write_proposal_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        if proposal is None:
            return False, "no-proposal"
        path = write_proposal(
            skill_name=skill_name,
            skill_path=skill_path,
            trajectories=skill_trajectories,
            proposal_text=proposal,
            output_dir=output_dir,
            now=now,
            semantic_verdict=semantic_verdict,
            semantic_reason=semantic_reason,
        )
        written_path["value"] = str(path)
        return True, str(path)

    def write_stub_node(ctx: dict[str, Any]) -> tuple[bool, str]:
        # over-cap + no proposal path: persist a stub so the next run
        # (or a human reviewer) sees the skill has been considered.
        if not (over_cap and proposal is None):
            return False, "no-stub-needed"
        stub = (
            "No clear improvement found.\n\n"
            f"Skill is {len(skill_text.encode('utf-8'))} bytes "
            f"(cap: {MAX_SKILL_BYTES}). The model could not "
            "propose a safe trim that preserves the primary "
            "workflow. Consider a manual review: the skill "
            "may have grown organically and the trim surface "
            "is unclear without domain context."
        )
        path = write_proposal(
            skill_name=skill_name,
            skill_path=skill_path,
            trajectories=skill_trajectories,
            proposal_text=stub,
            output_dir=output_dir,
            now=now,
            semantic_verdict="",
            semantic_reason="",
        )
        written_path["value"] = str(path)
        return True, str(path)

    dag: list[Node] = [
        Node(id="has_proposal", kind="gate", payload={"fn": proposal_present}),
        Node(id="semantic", kind="gate", payload={"fn": semantic_passed}),
        Node(id="tests", kind="gate", payload={"fn": tests_passed}),
        Node(id="write", kind="gate", payload={"fn": write_proposal_node}),
        Node(id="write_stub", kind="gate", payload={"fn": write_stub_node}),
    ]
    edges: list[Edge] = [
        Edge(src="has_proposal", dst="semantic", when="ok"),
        Edge(src="has_proposal", dst="write_stub", when="fail"),
        Edge(src="semantic", dst="tests", when="ok"),
        # semantic fail → no-op (chain ends). The proposal is dropped.
        Edge(src="tests", dst="write", when="ok"),
        Edge(src="tests", dst="write_stub", when="fail"),
    ]

    label = f"skillevo:{skill_name}"
    ctx = run_dag(dag, edges, job="skill_evolution", label=label)

    if written_path["value"]:
        return Path(written_path["value"])
    return None


async def run_evolution_pass(
    *,
    since_days: int = 7,
    skills_root: Path | None = None,
    output_dir: Path | None = None,
    env: dict[str, str] | None = None,
    ollama_settings: OllamaSettings | None = None,
    model: str = "kimi-k2.7-code:cloud",
    min_sessions: int = 1,
    enable_test_gate: bool = False,
    enable_semantic_check: bool = True,
    tests_root: Path | None = None,
    now: datetime | None = None,
    retention_months: int | None = DEFAULT_RETENTION_MONTHS,
) -> list[Path]:
    """Mine trajectories and write skill proposals. Returns written paths.

    Tail step: if ``retention_months`` is set, prune
    ``~/.ciao/trajectories/YYYY-MM/`` dirs older than that window. Pass
    ``retention_months=None`` to disable the prune (used in tests so the
    fixtures aren't deleted mid-suite).

    When ``skills_root`` is not provided, the resolver searches
    ``skills/`` first, then ``.claude/skills/`` (the read-only mirror
    maintained by ``ciao/upgrade.py``). This is needed because not every
    skill lives in the canonical root, and the prior behaviour silently dropped them
    from the evolution pass.

    Per-skill pipeline: each flagged skill is processed by
    :func:`_process_skill_dag`, which runs ``has_proposal`` →
    ``semantic`` → ``tests`` → ``write`` (or ``write_stub`` on
    over-cap-with-no-proposal) as a DAG via :mod:`ciao.dag`. Per-node
    timing lands in ``.runtime/job_runs.jsonl`` with label
    ``skillevo:<skill>:<node>``.
    """
    output_dir = output_dir or _DEFAULT_PROPOSALS_DIR
    if env is None:
        env = routine_env_for_model(model, ollama_settings) if ollama_settings is not None else {}
    now = now or datetime.now(UTC)
    since = now - timedelta(days=since_days)

    trajectories = _load_trajectories(since=since)
    flagged = find_underperforming_skills(
        trajectories, min_sessions=min_sessions
    )

    if skills_root is not None:
        search_roots: tuple[Path, ...] = (skills_root,)
    else:
        search_roots = _DEFAULT_SKILLS_ROOTS

    written: list[Path] = []
    if flagged:
        for skill_name, skill_trajectories in sorted(flagged.items()):
            skill_path = find_skill_file_in_roots(skill_name, search_roots)
            if skill_path is None:
                logger.info(
                    "Skill %s not found under any of %s; skipping",
                    skill_name,
                    [str(r) for r in search_roots],
                )
                continue
            try:
                path = await _process_skill_dag(
                    skill_name,
                    skill_path,
                    skill_trajectories,
                    output_dir=output_dir,
                    env=env,
                    model=model,
                    now=now,
                    enable_test_gate=enable_test_gate,
                    enable_semantic_check=enable_semantic_check,
                    tests_root=tests_root,
                )
            except Exception:
                # The DAG helper logs the failing node via job_runs;
                # re-raise logged, then continue with the next skill
                # (don't take the whole weekly pass down for one bad
                # skill).
                logger.exception(
                    "Skill %s DAG raised; continuing with next skill",
                    skill_name,
                )
                continue
            if path is not None:
                written.append(path)
    else:
        logger.info(
            "Skill evolution: no underperforming skills in last %d day(s)",
            since_days,
        )

    # Tail step: enforce retention so old trajectory months don't pile up.
    if retention_months and retention_months > 0:
        pruned = prune_old(retention_months=retention_months, now=now)
        if pruned:
            logger.info(
                "Pruned %d trajectory file(s) older than %d months",
                pruned,
                retention_months,
            )

    return written


def _load_trajectories(*, since: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in list_trajectories(since=since):
        try:
            out.append(load_trajectory(path))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable trajectory %s: %s", path, exc)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mine trajectories and write skill-edit proposals.",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=7,
        help="trajectory window in days (default: 7)",
    )
    parser.add_argument("--skills-root", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--model",
        default=None,
        help="model to call for proposals (Ollama / OpenRouter / Anthropic). Defaults to the runtime skill_evolution_model setting.",
    )
    parser.add_argument(
        "--min-sessions",
        type=int,
        default=1,
        help="only propose for skills with at least N flagged sessions",
    )
    parser.add_argument(
        "--test-gate",
        action="store_true",
        help="run tests for the skill before writing a proposal",
    )
    parser.add_argument(
        "--no-semantic-check",
        dest="semantic_check",
        action="store_false",
        help="skip the LLM-as-judge drift check (default: on)",
    )
    parser.set_defaults(semantic_check=True)
    parser.add_argument(
        "--retention-months",
        type=int,
        default=DEFAULT_RETENTION_MONTHS,
        help=(
            "prune trajectory months older than this at the end of the run "
            f"(default: {DEFAULT_RETENTION_MONTHS}; 0 disables)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list flagged skills without calling the model",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.dry_run:
        since = datetime.now(UTC) - timedelta(days=args.since_days)
        trajectories = _load_trajectories(since=since)
        flagged = find_underperforming_skills(
            trajectories, min_sessions=args.min_sessions
        )
        search_roots = (
            (args.skills_root,) if args.skills_root else _DEFAULT_SKILLS_ROOTS
        )
        for name, recs in sorted(flagged.items()):
            sids = ",".join((r.get("session_id") or "")[:8] for r in recs)
            print(f"{name}: {len(recs)} session(s) -> {sids}")
            skill_path = find_skill_file_in_roots(name, search_roots)
            if skill_path is None:
                print(
                    f"  ! {name}: no SKILL.md found under any of "
                    f"{[str(r) for r in search_roots]} (slash command, "
                    f"agent, or external)",
                    file=sys.stderr,
                )
            else:
                size = skill_path.stat().st_size
                if size > MAX_SKILL_BYTES:
                    print(
                        f"  ~ {name}: skill is {size} bytes "
                        f"(>{MAX_SKILL_BYTES}); will run trim-mode proposal",
                        file=sys.stderr,
                    )
        print(
            f"\n{len(flagged)} flagged skill(s) "
            f"across {len(trajectories)} trajectories",
            file=sys.stderr,
        )
        return 0

    # Record the weekly pass as a job run. This runs as a subprocess, so
    # pin the recorder to the repo's .runtime (honouring an env override)
    # rather than relying on the process cwd.
    from ciao import job_runs

    job_runs.configure(
        os.environ.get("CIAO_RUNTIME_ROOT")
        or os.environ.get("TELEGRAM_BRIDGE_RUNTIME_ROOT")
        or (Path(__file__).resolve().parents[1] / ".runtime")
    )
    with job_runs.track_sync(
        "skill_evolution", "Skill evolution", model=args.model
    ) as run:
        from ciao.config import CiaoConfig
        from ciao.providers.routing import resolve_with_fallback
        cfg = CiaoConfig.from_env()
        if args.model is None:
            args.model = os.environ.get("CIAO_MODEL") or cfg.claude_default_model or "sonnet"
        args.model, env, note = resolve_with_fallback(
            args.model, cfg, default_model=cfg.claude_default_model
        )
        if note:
            run.extra["fallback"] = note
            logger.info("Skill evolution %s", note)
        paths = asyncio.run(
            run_evolution_pass(
                since_days=args.since_days,
                skills_root=args.skills_root,
                output_dir=args.output_dir,
                env=env,
                model=args.model,
                min_sessions=args.min_sessions,
                enable_test_gate=args.test_gate,
                enable_semantic_check=args.semantic_check,
                retention_months=args.retention_months or None,
            )
        )
        run.extra["proposals"] = len(paths)
        if not paths:
            run.skip("no proposals written")
    print(f"Wrote {len(paths)} proposal(s)")
    for p in paths:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
